# OuroborosHub

**OuroborosHub is the official skills catalog for [Ouroboros](https://github.com/razzant/ouroboros).**

Ouroboros is a self-modifying desktop AI agent. Skills extend it with new tools, HTTP routes, widgets, scripts, and task-specific capabilities. This repository is the curated catalog the desktop app reads when you open **Skills -> OuroborosHub**.

Each catalog entry lists the files that belong to a skill and their SHA-256 hashes. Ouroboros downloads those files from this repository, verifies the hashes, runs its skill review, and only then lets the owner enable the skill.

## What is in this hub?

Current examples include:

| Skill | Type | What it does |
| --- | --- | --- |
| `weather` | extension | Live weather widget using `wttr.in`, no API key required. |
| `nanobanana` | extension | Image generation widget through OpenRouter image models. |
| `music_gen` | extension | Music generation widget through OpenRouter / Google Lyria. |
| `video_gen` | script | Video generation script skill. |
| `duckduckgo` | extension | Zero-key DuckDuckGo web search. |
| `perplexity` | extension | OpenRouter-backed deep web research with citations. |

The source of truth is [`catalog.json`](catalog.json). The actual skill payloads live under [`skills/`](skills/).

## For users

You normally do not need to clone this repository.

1. Install or run [Ouroboros Desktop](https://github.com/razzant/ouroboros).
2. Open **Skills**.
3. Open the **OuroborosHub** tab.
4. Pick a skill and click **Install**.
5. Wait for the security review to finish.
6. Enable the skill when it has a fresh `PASS` review.

Some skills may ask for owner-approved key grants before they can run. For example, `perplexity` needs `OPENROUTER_API_KEY`. Ouroboros handles that through the Skills UI and the desktop owner-confirmation flow.

## For skill authors

Start in the main Ouroboros repository:

- [Creating Skills for Ouroboros](https://github.com/razzant/ouroboros/blob/ouroboros/docs/CREATING_SKILLS.md) - practical author guide for `SKILL.md`, `PluginAPI`, widgets, review, grants, and publishing.
- [Skill Review Checklist](https://github.com/razzant/ouroboros/blob/ouroboros/docs/CHECKLISTS.md#skill-review-checklist) - what the reviewer models check before a skill can pass.
- [Architecture Reference](https://github.com/razzant/ouroboros/blob/ouroboros/docs/ARCHITECTURE.md) - how the skill loader, review state, extension loader, widgets, and marketplace surfaces work.

Skills are reviewed code. Keep them small, explicit, and honest about permissions.

Module dashboards can declare `render.appearance: host` and feature-detect the
optional `OuroborosWidget.onTheme` API. On hosts with the bridge, apply the
resolved Light/Dark value without rebuilding the widget and unsubscribe during
disposal. On older hosts, preserve a usable local palette; the declaration alone
does not enable theme delivery. Widgets with their own appearance can declare
`independent` or `fixed` (an intentionally stable visual world).

Changed published payloads need a new skill version in `SKILL.md` and the catalog:
existing clients discover updates by comparing those versions. Test both the
current bridge and a host without `onTheme`, including Canvas/SVG repaint and
preserved state. The core author guide above owns the detailed contract.

## How to add a skill

A skill lives in its own folder:

```text
skills/<slug>/
  SKILL.md
  plugin.py          # for type: extension
  scripts/run.py     # for type: script
  widget.js          # optional, only for reviewed kind: module widgets
```

Then update `catalog.json` so Ouroboros can discover and verify it.

The easiest path is:

```bash
python scripts/build_catalog.py
```

That script walks `skills/*`, reads the frontmatter from each `SKILL.md`, computes SHA-256 hashes and file sizes, and rewrites `catalog.json`.

You can also edit `catalog.json` manually when needed. A minimal entry looks like this:

```json
{
  "slug": "example_skill",
  "name": "example_skill",
  "description": "Short, user-facing description.",
  "version": "0.1.0",
  "type": "extension",
  "files": [
    {
      "path": "SKILL.md",
      "sha256": "<sha256>",
      "size": 1234
    },
    {
      "path": "plugin.py",
      "sha256": "<sha256>",
      "size": 5678
    }
  ]
}
```

File paths are resolved as:

```text
https://raw.githubusercontent.com/razzant/OuroborosHub/main/skills/<slug>/<path>
```

`SKILL.md` must be present for every skill.

Before submitting a change, check the published catalog without rewriting it:

```bash
python -m pip install PyYAML pytest
python scripts/build_catalog.py --check
python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q skills/cache_efficiency_snapshot/test_cache_efficiency.py
```

The check verifies the catalog structure, installation-name collisions and every
declared file's SHA-256 and byte size when supplied. Slugs are compared using
Ouroboros's canonical installation names; display names may be shared. Curated
metadata, formatting and intentionally unlisted development files are preserved.
The same validation runs before normal catalog generation writes its result.
Pull requests and pushes to `main` run these checks in CI.

The dashboard browser acceptance needs a local Ouroboros core checkout (including
its Git history), Playwright, Chromium and WebKit. Run it explicitly; ordinary
catalog CI does not provide these dependencies:

```bash
OUROBOROS_THEME_CORE_ROOT=/path/to/ouroboros python -m pytest -p no:cacheprovider -q tests/test_dashboard_theme_browser.py
```

It uses real current and pre-theme module bridges with synthetic skill responses,
checks both palettes, state preservation, Canvas/SVG repaint and disposal, and
confirms that an older bridge keeps the dark fallback.

The Smart Lists unit suite is included in catalog CI. Its real module-bridge
browser checks are opt-in and use isolated skill state (not installed skills):

```bash
python -m pytest -q skills/smart_lists/tests
OUROBOROS_THEME_CORE_ROOT=/path/to/ouroboros python -m pytest -q tests/test_smart_lists_browser.py
```

The optional core manifest validator in the unit suite uses `OUROBOROS_CORE_ROOT`;
its absence is a disclosed skip, not a successful core-validation check.

## Review and trust model

OuroborosHub is curated, but hub membership does not bypass review.

Every installed skill still goes through the normal Ouroboros lifecycle:

```text
install -> review_skill -> isolated deps (if any) -> owner enable -> execute/dispatch
```

Important constraints:

- Skill payloads are text-only. Do not vendor binary blobs, `.so`, `.dylib`, `.dll`, `.wasm`, `.pyc`, `.node`, model weights, or generated caches inside `skills/<slug>/`.
- Core setting keys such as `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, or `TELEGRAM_BOT_TOKEN` require explicit owner grants after a fresh `PASS` review.
- Dependencies should be declared through the supported skill metadata path and installed into the isolated per-skill environment by Ouroboros after review.
- Widgets should prefer host-owned declarative components. Custom `kind: "module"` widgets are sandboxed and get an extra `widget_module_safety` review item.
- Provenance files, review state, grants, dependency fingerprints, and enablement state are owner/review controlled. Skills should not write them.

## Repository layout

```text
catalog.json                 # public catalog consumed by Ouroboros
scripts/build_catalog.py      # rebuilds catalog hashes from skills/*
skills/weather/               # example extension skill
skills/duckduckgo/            # search extension
skills/perplexity/            # OpenRouter research extension
skills/<slug>/SKILL.md        # every skill starts here
```

## Related repositories

- [Ouroboros Desktop](https://github.com/razzant/ouroboros) - the main application.
- [Creating Skills for Ouroboros](https://github.com/razzant/ouroboros/blob/ouroboros/docs/CREATING_SKILLS.md) - skill authoring guide.
- [Original Ouroboros](https://github.com/joi-lab/ouroboros) - the earlier Colab/Telegram version.

## License

Skills in this repository should declare their own license in `SKILL.md` when relevant. The repository itself is maintained by Joi Lab as the official Ouroboros skills catalog.
