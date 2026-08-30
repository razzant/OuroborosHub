---
name: telegram-bot
description: Durable Telegram transport for generic Ouroboros presences, with exact actor and conversation provenance, media staging, and provider receipts.
version: 0.2.0
type: extension
runtime: python3
entry: plugin.py
permissions:
  - net
  - fs
  - read_settings
  - supervised_task
  - route
  - widget
  - tool
  - presence
env_from_settings:
  - TELEGRAM_PUBLIC_BOT_TOKEN
timeout_sec: 60
when_to_use: The owner wants a reviewed Telegram bot transport for one or more external presence bindings, separate from the native owner-control Telegram bridge.
ui_tab:
  tab_id: transport
  title: Telegram Transport
  icon: message
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: poll
        route: status
        method: GET
        interval_sec: 5
        components:
          - type: callout
            path: runtime_state
            tone: info
          - type: group
            title: Provider custody
            layout: grid
            columns: 4
            components:
              - type: metric
                label: Inbox waiting
                path: inbox_waiting
              - type: metric
                label: Inbox leased
                path: inbox_leased
              - type: metric
                label: Submitted
                path: inbox_submitted
              - type: metric
                label: Inbox failed
                path: inbox_failed
              - type: metric
                label: Outbox waiting
                path: outbox_waiting
              - type: metric
                label: Delivered
                path: outbox_delivered
              - type: metric
                label: Outbox failed
                path: outbox_failed
              - type: metric
                label: Telegram offset
                path: telegram_offset
          - type: kv
            fields:
              - label: Bot
                path: bot_label
              - label: Last provider event
                path: last_event_at
              - label: Last delivery
                path: last_delivery_at
              - label: Last error
                path: last_error
tools:
  - name: telegram_send
    description: Queue a proactive Telegram text, photo, or document for durable delivery.
---

# Telegram Bot Presence Transport

This extension owns Telegram provider custody. It is deliberately separate from
the bundled owner-control Telegram bridge.

It provides:

- exact Telegram actor, chat, topic, message, reply, and attachment provenance;
- a SQLite inbox/outbox with stable event ids, leases, deduplication, bounded
  delivery retries, terminal failure state, and an offset committed in the same
  transaction as each accepted update;
- inbound photo/document staging through Telegram `getFile`;
- outbound text, photo, and document provider helpers with durable receipts;
- a namespaced `telegram_send` tool for proactive text/photo/document delivery
  to exact numeric chat, topic, and message ids;
- a bounded poller and fixed worker set; and
- an operational Widget showing provider and custody state.

The payload does not call the owner `/chat/inject` path, allocate synthetic
internal chats, interpret owner commands, or create prompt envelopes. It uses
only the reviewed `presence` Host permission. Configure the single opaque,
owner-created account-wide binding (`conversation_id="*"`) as `binding_id` in
this skill's local `settings.json`; inbound Telegram text is never treated as
configuration. The adapter submits the actual provider conversation facts to the
loopback Host and durably polls any deferred work before delivering its late text
once.

Supported v1 Telegram content is text/caption, photos, and documents. Voice,
reactions, edited messages, service events, and Mini App behavior are outside
this transport.
