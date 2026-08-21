---
name: slack-bridge
description: Bidirectional Slack presence transport for Ouroboros using Socket Mode, durable delivery queues, threaded replies, proactive text messages, and inbound file staging.
version: 1.0.0
type: extension
entry: plugin.py
runtime: python3
permissions: [net, read_settings, widget, route, tool, companion_process, presence]
env_from_settings: [SLACK_BOT_TOKEN, SLACK_APP_TOKEN]
dependencies: [httpx, websockets]
when_to_use: User wants Ouroboros to receive Slack messages or files, reply in Slack threads, or send proactive Slack text messages.
timeout_sec: 30
companion_processes:
  - name: slack_socket_mode
    command: [python3, scripts/slack_daemon.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 10
tools:
  - name: slack_send
    description: Queue a proactive Slack text message or threaded reply for durable delivery.
---

# Slack Bridge

Slack Bridge is a provider-neutral Slack transport. It receives Slack events
through Socket Mode, commits every acknowledged envelope to a local SQLite
queue, preserves exact Slack provenance, stages inbound private files with the
bot credential, and durably delivers text replies.

The skill does not decide who is an administrator, reinterpret slash commands,
or turn Slack messages into owner commands. It transports conversation events
and leaves identity, authority, memory, and turn policy to the host presence
runtime.

## Presence binding

Choose the owner-created account-wide presence binding in this skill's settings.
Create it for provider `slack`, the workspace Team ID shown in the widget, and
conversation ID `*`. The bridge keeps that one 32-character lowercase
hexadecimal Binding ID and submits neutral provider events to the reviewed
loopback presence endpoint using the dedicated `presence` permission. Immediate
text is queued once for Slack; deferred work keeps its durable work reference and
is polled until terminal.

## Slack app setup

1. Create a Slack app from `manifest.json` and enable Socket Mode.
2. Create an app-level token with `connections:write` and save it as
   `SLACK_APP_TOKEN`.
3. Install the app and save its bot token as `SLACK_BOT_TOKEN`.
4. Grant both settings to this reviewed skill, then enable it.
5. Save the owner-created Presence Binding ID in the skill settings.
6. Invite the bot to channels where it should participate.

Every DM, MPDM, public channel, and private channel event that the installed app
can actually receive is admitted. Invite the bot where Slack requires explicit
membership; there is no second bridge-local channel allowlist.

Inbound Slack files are downloaded from their authenticated `url_private`
locations into the skill state directory before the host adapter sees them.
Outbound file upload is intentionally not part of this version; the Slack app
does not request `files:write`.

## Delivery behavior

- Socket envelopes are acknowledged only after their durable SQLite transaction
  commits.
- Slack retry envelopes and duplicate event IDs are deduplicated.
- Expired leases are reclaimed after a crash.
- Work is bounded and ordered per Slack thread while independent threads may run
  concurrently.
- An outbound item becomes terminally failed after five delivery attempts; that
  failed item no longer blocks later messages in the same Slack thread.
- Long outbound text is split into Slack-safe chunks before it enters the
  durable outbox.
- The Widgets tab reports connection state, queue depth, failures, and recent
  activity without exposing tokens or message contents.
