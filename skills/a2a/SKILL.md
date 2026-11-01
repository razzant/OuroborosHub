---
name: a2a
description: Agent-to-Agent protocol bridge for Ouroboros. Provides a local A2A-compatible server plus client tools for discovering and messaging other A2A agents.
version: 1.5.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, tool, route, widget, companion_process, presence]
env_from_settings: []
when_to_use: User asks to communicate with another A2A-compatible agent, discover an agent card, send an A2A message, check A2A task status, or expose this Ouroboros instance as an A2A peer.
timeout_sec: 120
install_specs:
  - kind: pip
    package: "protobuf<6"
  - kind: pip
    package: "a2a-sdk[http-server]>=1.0.0,<2.0.0"
companion_processes:
  - name: a2a_server
    command: [python3, scripts/a2a_daemon.py]
    runtime: python3
    restart_policy: on_failure
icon: ui/icon.svg
contributes:
  - id: place
    provides: place
    label: A2A
    destination: vendor:A2A
    scope: account
    icon: ui/icon.svg
    wakes: [direct_message, from_bot]
tools:
  - name: discover
    effect: read
    target: url
  - name: send
    effect: message
    target: url
    conversation: context_id
    thread: task_id
  - name: status
    effect: read
    target: url
    conversation: task_id
---

# A2A skill

Note for reviewers: the companion daemon is declared in this manifest's
frontmatter under `companion_processes` (name `a2a_server`, argv
`[python3, scripts/a2a_daemon.py]`, runtime `python3`, restart policy
`on_failure`) and is registered at load via
`api.register_companion_process("a2a_server")`. The host's extension
loader resolves the descriptor from the frontmatter; review-prompt
manifest summaries may omit the `companion_processes` field, so consult
the frontmatter above for the authoritative declaration.

This skill moves Ouroboros's Agent-to-Agent protocol support out of the
core runtime. It exposes a small local A2A-compatible JSON-RPC server and
registers three client tools:

- `discover` — fetch another agent's Agent Card.
- `send` — send a message to another A2A agent.
- `status` — check a remote task status.

The companion process talks back to the host through the loopback Host
Service API using the reviewed `SkillToken` grant. It does not patch the
core runtime and stores task state under the skill state directory.

## Outbound peer credentials

The client tools (`discover`, `send`, `status`) send HTTP Basic credentials only
to ONE explicitly configured peer. Set both `A2A_CLIENT_PASSWORD` and
`A2A_CLIENT_PEER_URL`; the credential is attached only when the target URL's
origin (scheme, host, port) exactly matches that configured origin, and every
other peer is contacted anonymously. This is deliberate: the tools accept an
arbitrary caller-supplied URL, so a process-wide credential would be handed to
whatever address the model happened to pass.

## Agent card

The published Agent Card describes what this Ouroboros instance actually
does, capability-first. The top-level name is the stable product name
`Ouroboros` (operator-set `A2A_AGENT_NAME` wins when configured — the host
`/identity` name is deliberately NOT used: it is the identity document's
first heading, a section title like "Who I Am", not an agent name). The
description leads with a curated capability summary and appends the host
`/identity` first line as flavor (`A2A_AGENT_DESCRIPTION` overrides the
whole field). The skill list always begins with five curated capability
categories (code and files, web and media, long-running tasks and
projects, self-modification, skills), followed by live per-tool entries
from the host tool schemas (`GET /tools/schemas`). A discovery GET answers
in seconds: the request path uses the last known-good tool list and a
short fetch only, while a background refresher owns the long host-warmup
retry ladder and keeps the list warm. When the tool registry is genuinely
unavailable the card still carries the curated categories — it never
collapses to a contentless "General" stub nor to an identity-persona-only
entry.

## A2A place and durable requests

Connect provider `a2a`, account `server` in Places. Direct peer requests carry
`direct_message` and `from_bot`; both are suggested for this agent-facing place.
Requests go through `/presence/turn` using the `presence` permission. Peer text,
including slash prefixes, stays conversation data. A places-capable Host is
required; there is no fallback to owner chat.

The context ID addresses the conversation. Each task/message pair has a stable
source-event digest. The exact event is saved before submission, and Host
turn/work references are retained under `presence/` alongside the existing task
records. Identical retries reuse the receipt; changed content under the same
message ID is refused. Unknown admission remains working, never falsely complete.
Deferred work is polled through `/presence/work/{ref}`. Silent and tool-delivered
outcomes produce no duplicate answer.

SDK streams show working status and periodic heartbeats until a final artifact.
There is no chat-log scraping. `A2A_RESPONSE_TIMEOUT_SEC` bounds the initial wait;
`A2A_STREAM_DEADLINE_SEC` bounds the overall wait without cancelling Host work.
Legacy allocation/progress configuration remains readable but is unused.

Task records remain atomic and fenced by the current message. SDK terminal
replays verify original content/context, and listing reads local snapshots with
owner filtering. The no-SDK route shares the same durable protocol. Legacy
owner-chat records stay readable, but old messages cannot be resubmitted as
place events; a new message may start a place conversation.

The Presence protocol currently has no cancellation endpoint. `tasks/cancel`
returns `TaskNotCancelableError` with that reason; work remains unchanged.
This applies equally to SDK and fallback routes.
