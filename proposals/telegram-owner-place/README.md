# Draft: owner Telegram bridge with private-chat Places and curated Project news

**Source proposal for discussion — not ready to merge, not an installable Hub release.**

This preserves the current native `telegram` 1.4.0 implementation, rather than
silently renaming it into a different connector. It is deliberately outside
`skills/` and has **no entry in `entries/`, `catalog.json` or `official.json`**.
It does not replace or modify the existing `telegram-bot` or
`telegram-user-session` skills. Do not copy this over a running installation.

## Why an unlisted proposal?

The [Hub README at the submission base](https://github.com/razzant/OuroborosHub/blob/2ce18939cc8b51255d2e9f48ddfd5eb32e08e0c5/README.md)
distinguishes the bundled owner's bridge from the Hub's bot and user-session
connectors. `telegram-bot` is now **0.7.0** and supports private chats as well as
groups and channels; private-chat support alone is not the distinction here.
This proposal combines the existing **owner/control bridge** with a real Place
conversation in the **same bot and receiver**, plus editorial Project news.

The native code is coupled to host internals and retains the built-in `telegram`
identity. A new name is not a manifest-only edit: for example,
`lib/telegram_actions.py::_source` checks `transport_skill == "telegram"`.
The Hub's current connector tests also require explicit tool destinations,
exclude `inject_chat`, and require a `ui/icon.svg` connector mark; this native
snapshot does not meet those standalone-connector expectations. The proposal
changes none of those rules.

Putting an entryless or withdrawn snapshot under `skills/telegram` would not
keep it unlisted: `scripts/build_index.py::build_catalog` enumerates every
immediate `skills/*/SKILL.md` into the legacy catalog. Keeping the source under
`proposals/` avoids that accidental release. Maintainer agreement on the right
home and supported host boundary comes before an installable conversion.

## What is in the snapshot

- **One receiver:** existing owner commands, emergency control and quiz answers
  keep the bridge path; ordinary private messages can use a connected Place.
- **A separate Place conversation:** provider message identity, incoming Reply,
  selected quote metadata and supported attachment intake are retained. The
  Place's history is not a mechanical merge with Main.
- **Exact owner handoff:** `hand_to_main` passes retained owner words to ordinary
  Main routing. It checks the calling focus, the pinned owner, current Place
  account confirmation and the selected source; the model supplies no replacement
  owner text. Forwarded messages and unknown old bot references are refused.
- **Native conversation actions:** queued send, edit, reaction and receipt-read
  tools, with request identity and conservative handling of uncertain sends.
- **Curated Project news:** root-authored Project messages and media references
  wait for an authorized mind to publish a summary or skip them with a reason.
  Ordinary Main dialogue and direct helper output are not news sources. Capture
  alone neither starts an editor/model nor sends a digest.
- **Separate delivery choices:** raw mirroring is independent of Place replies
  and selected news. `main` delivers Main originals with Project/helper filtering;
  `telegram_only` suppresses web-originated Main replies while retaining
  Telegram-originated return responses and owner reminders/skill notices.
  The shipped default remains `all`; this proposal exports no local settings.
- **Retained custody:** input order, staged media, canonical output identities,
  per-part delivery reports and held/uncertain outcomes survive through the
  existing state paths. This is not an exactly-once guarantee.

The existing Mini App code is included because this is an exact full payload
snapshot. **This contribution does not repair or newly qualify the Mini App.**

## Provenance and contents

| Material | Origin |
| --- | --- |
| `payload/telegram/` | All **24 tracked payload files**, byte-identical to the installed native 1.4.0 snapshot at capture |
| `source-manifest.json` | Per-file Git blob identity, SHA-256, byte count and exact source commits |
| `host-media-provenance.patch` | Original narrow host event/test delta carrying producing-task and row identity on photo, video, document and link events; reference only |
| `host-tests.patch` | Original Telegram-related test changes; excludes the message-bus tests already in the other patch |
| `LICENSE` | Original MIT license and copyright notice, retained from the source repository |

Exact source lineage:

- Public upstream base: `06fc0c067cbc58315452d50c0ac8e6b5a4d1ec58`.
- Local Telegram feature commit: `ea15396d37bf32a372e946daff88918f12ccee6b`.
- Captured installed checkout: `7f86c7b8c83073ec507b6b07f686510072fd3f3c`.
  All Telegram payload blobs are unchanged from the feature commit; unrelated
  host-action UI changes in this later checkout are **not** included.
- Native payload release digest:
  `93373c75ce551bd633350122e1c864ba62b5eeb049fcb8ffe035f6c687683a8d`.

The two local commit IDs identify provenance, not publicly available branches
or a declared minimum host version. This proposal contains the payload bytes
and reference patches themselves. Installation markers, bot tokens, account/chat
IDs, settings, databases, queues, private messages, logs and reviewer transcripts
are not included. The original implementation is authored by Ouroboros on top
of the MIT-licensed Ouroboros codebase; upstream authorship is preserved.

## Host coupling and compatibility work

This is not a standalone PluginAPI-only package. Direct host dependencies include:

- `task_results`, `task_status`, `owner_mailbox`, `presence_runner` and `places`
  for retained source and current owner-confirmation facts;
- `dialogue_store`, `chat_chain`, `projects_registry`, `project_dialogue` and
  `delegate_activity` for room, lineage, history and presentation facts;
- `gateway.reply_preparation`, `gateway.files`, `i18n_memory`, `ui_language`,
  `ui_translation`, `net_transport`, configuration and utility helpers;
- private helpers `supervisor.steering._relayed_owner_message` and
  `workspace_patch_rules._sensitive_untracked_reason`.

The source also uses the existing authenticated Host Service identity, chat,
Presence, continuation and delivery routes. `accepted_chat_message` is an
existing re-export in `supervisor.message_bus`, not a missing symbol.

The media provenance patch describes the tested host composition. Older hosts
can omit producing-task facts and keep the documented older media behavior;
that fallback is **not proof of equivalent helper filtering**. No minimum
public release, compatibility with today's moving Development tip, standalone
Hub installation, or fresh-install migration has been qualified here.

## Evidence: distinguish the original implementation from this publication

The original local integration recorded selected whole-delta verification on
26 October 2026: **195 Python test modules plus the Node lane**, including
explicitly widened indirect consumers. This was **not the full battery**.
Settings save/reload flows were exercised in Chromium and WebKit at desktop and
narrow widths. Synthetic Telegram/model fixtures were used for functional tests.
A later real private-chat greeting/response and incoming Reply were observed
on the installed integration; selected quotes and existing-Project handoff
remain unqualified through a live owner exchange.

An independent reviewer found critical defects on an earlier candidate.
They were corrected and tested; the final implementation was accepted by its
author under Advisory. **There is no fresh independent PASS for the final
Telegram implementation.** The original commit also used a disclosed narrow
Cyber author-attribution exception after Resume lost the candidate baseline;
it did not waive tests or rewrite the earlier critic verdict.

This PR is publication of that retained work, not a new functional rewrite or
claim that Hub CI exercises it. The reference test patch requires the original
Ouroboros test harness and belongs in a disposable host checkout. It is not a
standalone Hub test suite and must not be applied to a running installation.
Publication checks can establish source equality, syntax, catalog exclusion
and absence of detected secrets; they do not establish runtime compatibility.

## Explicit gaps before any release

- No Place typing indicator, group-Place support, speech recognition or automatic
  digest schedule is added by this snapshot. News remains deliberate selection.
- Private chat history is currently a read-only Place-history view in Settings,
  not an editable sidebar chat.
- An unreadable custody store stops polling before acknowledgement; Telegram
  controls are then unavailable and app controls are required. A permanently
  refused Main-relay head can hold that chat.
- Setting ordinary messages back to `main` affects new messages only; retained
  Place work and its outbox keep their original route. Silent/Paused or disconnect
  holds queued Place delivery separately. No state reset is proposed.
- Unknown external send outcomes remain uncertain rather than being replayed.
- Native Windows, physical mobile clients, broad restart/fault scenarios and
  full-suite qualification remain open. The separate host account-confirmation
  card work is not part of this proposal and its known edge cases are not fixed here.

Before marking this ready: agree whether the owner integration belongs in the
built-in source or can be extracted behind supported host contracts; settle
identity and one-receiver migration/conflicts; qualify the target host/version
and real owner flows; then perform an appropriate independent review and only
then add release metadata/catalog entries. This draft does not authorize any
of those changes automatically.
