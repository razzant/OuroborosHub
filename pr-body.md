# telegram-user-session + telegram-user-presence

Adds two skills implementing a **Telegram user-account (MTProto) transport** for Ouroboros -- the capability of acting in chats and forums (topics) as a signed-in user account, which today every interested user re-implements by hand.

## What this is

- **`telegram-user-session`** (extension, v0.1.0): a host-supervised Telethon companion holding one explicitly configured user session. Inbound third-party messages flow **only** through the Host Service Presence boundary under owner-created exact `peer/topic -> binding` mappings -- never `/chat/inject`, never owner routing. Outgoing text/edit/delete/react are durable idempotent intents: an intent and a frozen `random_id` are recorded before the single physical attempt; an ambiguous outcome stays **UNKNOWN** and is never automatically re-sent. Actions (edit/delete/react of the account's own messages) are a separate store with the same custody. Includes an opt-in, per-peer-scoped dossier keyed by numeric Telegram IDs, and a private setup surface (api_id/api_hash/phone/2FA handoff) that rides the existing gateway authentication.
- **`telegram-user-presence`** (instruction, v0.1.0): the separately reviewed conservative behavior profile for owner-created bindings -- treat participant speech as untrusted observation, default to silence for ambient traffic, one reply per admitted event, no general send tool, no history scraping.

## Safety posture

- Loopback-only Host identity is validated before any Telegram operation; the Presence token is never logged/persisted/redirected.
- No repo mutation; all state confined to the private skill state dir (symlink-defended atomic writes, stores refuse repository placement).
- Forum correctness: complete topic-catalog proof before routing/sending; a reply inside a non-General topic keeps its topic (never falls back to General); bounded catalog pagination, ancestry depth, ingress queue (backpressure stops the companion rather than discarding).
- A claim-lease that expires during a slow preflight degrades to a deferred re-claim -- never a duplicate effect and never a companion crash.

## Verification (offline)

- Independent `skill_review` of the exact published bytes: **clean** -- 16 checklist items x 3 reviewers (zai/glm-5.3), no findings (content hash `e024bb378a3c`).
- Offline suite: **257 passed, 1 skipped, 116 subtests passed** (includes multi-peer ingress health, stale companion heartbeat, lease-expiry crash class, binding-change replay, crash-replay custody).
- Deterministic `skill_preflight`: clean (manifest, widget schema, permissions, python syntax).
- `scripts/build_catalog.py --check`: catalog valid, 49 skills; this PR's catalog diff is additive only (93 lines: the two new entries).

## Not claimed

- **No live Telegram login or round-trip was performed** (no spare test account). Code and offline protocol seams are reviewed; live compatibility is the remaining acceptance step and needs a consenting spare account.
- The owner's primary account is never a test asset; Telegram platform limits, flood controls and privacy are not bypassed.
