---
name: telegram-user-presence
description: Conservative conversation behavior for an explicitly bound Telegram user-account transport.
version: 0.1.0
type: instruction
when_to_use: An owner-created Presence binding admits a Telegram user-account conversation through a separately reviewed transport.
model_experience:
  what_model_sees: Reviewed guidance for a non-owner conversation; the transport sends one immediate reply or an interim plus a correlated child reply to the exact admitted conversation, with separate provider delivery receipts.
  token_effect: The profile is loaded only for admitted Presence turns; no idle tool schema or background scan.
presence:
  schema_version: 1
  instructions_file: presence.md
  runtime_defaults:
    model_slot: light
    inline_max_rounds: 6
---

# Telegram user Presence behavior

This instruction-only skill governs an owner-created Presence binding. It does not authenticate Telegram, create a binding, grant a send tool, read private transport state, or turn a participant into the owner. The transport is a separate reviewed skill: its reply channel maps each model-authored answer or deferred interim/child answer to the exact admitted event's account, peer and topic. Do not grant its general-purpose `send_text` tool to this Presence binding; that tool takes arbitrary owner-allowlisted peer IDs and is for separately authorized owner tasks. Read [presence.md](presence.md). Installation or enablement alone does not authorize a reply.
