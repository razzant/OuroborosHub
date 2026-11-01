# Telegram group discovery: real-host/browser qualification (opt-in)

Runs the real current Ouroboros host (a byte-faithful, origin-proved copy of a core checkout), the Telegram bot
plugin payload under test, the plugin's own poll/capture against a controlled loopback Bot API fed raw synthetic
updates, and the owner's browser at Dashboard > Consciousness > Connect a place > Telegram. No installed state,
credential or real Telegram call. The plugin is admitted by synthetic fixture records (review state and grants
written for its content hash); no review is performed.

**Opt-in only.** CI maps only `skills/telegram-bot/tests`; nothing collects this directory by default.

```
export TG_DISCOVERY_CORE=<core candidate checkout>
export TG_DISCOVERY_VENV=<dependency-only venv with core requirements + playwright>
export PLAYWRIGHT_BROWSERS_PATH=<cache holding webkit>      # chromium too if TG_DISCOVERY_ENGINES=webkit,chromium
tests/telegram_group_discovery/run.sh <evidence-dir> [<plugin payload dir>]   # default: skills/telegram-bot
```

`run.sh` goes through the core's `safe_test` launcher and passes `TG_DISCOVERY_ENGINES` (default `webkit`, which
is required) and `TG_DISCOVERY_LEGACY_REF` (the published skill the update case starts from, default Hub
`089ea45`) inside it. Evidence (JUnit, JSON facts, host logs, screenshots at 1100x750 and 390x844, light and dark)
goes to `<evidence-dir>`.

Cases: provenance; groups offered by exact signed id; one chosen group connected Silent; repeat Open preserves
settings; membership/service add, remove, re-add and migration; no cap; unknown / known-empty / unreadable list;
a group pending under skill 0.8 offered after the update. Each engine gets a fresh host; each case sets up what
it needs and runs alone. Against skill 0.8 (no `status.connect`) the suite is the negative control and fails.
