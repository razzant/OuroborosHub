---
name: agentrq-bridge
description: "AgentRQ integration: structured HITL task board, permission gating, and real-time workspace bridge via MCP Streamable HTTP"
version: 0.3.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions:
  - net
  - tool
  - route
  - widget
  - read_settings
  - companion_process
  - presence
env_from_settings:
  - AGENTRQ_MCP_URL
  - AGENTRQ_TOKEN
when_to_use: >
  User wants to interact with an AgentRQ workspace: pull tasks, create tasks
  for human review, reply to task threads, update task status, or request
  human permission for sensitive actions.
companion_processes:
  - name: agentrq_listener
    command: [python3, listener.py]
    runtime: python3
timeout_sec: 120
ui_tab:
  tab_id: dashboard
  title: AgentRQ
  icon: tasks
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: poll
        route: status
        method: GET
        interval_sec: 15
        components:
          - type: kv
            fields:
              - label: Connected
                path: connected
              - label: Workspace
                path: name
              - label: Description
                path: description
              - label: Agent Online
                path: agent_connected
icon: ui/icon.svg
contributes:
  - id: place
    provides: place
    label: AgentRQ
    destination: vendor:AgentRQ
    scope: account
    icon: ui/icon.svg
    wakes: [direct_message]
tools:
  - name: rq_workspace
    effect: read
    target: []
  - name: rq_pull_next
    effect: write
    target: []
  - name: rq_create_task
    effect: write
    target: []
  - name: rq_reply
    effect: message
    target: task_id
    conversation: task_id
  - name: rq_update_status
    effect: write
    target: task_id
    conversation: task_id
  - name: rq_get_messages
    effect: read
    target: task_id
    conversation: task_id
---

# AgentRQ Bridge

Connects Ouroboros to an [AgentRQ](https://github.com/agentrq/agentrq)
workspace via the MCP Streamable HTTP transport. AgentRQ is a task
orchestration platform for structured human-agent collaboration.

## What it provides

- **6 agent tools** that map to the AgentRQ per-workspace MCP surface:
  `rq_workspace`, `rq_pull_next`, `rq_create_task`, `rq_reply`,
  `rq_update_status`, `rq_get_messages`.
- **Real-time SSE listener** — a companion process that holds a
  persistent SSE connection to the AgentRQ MCP endpoint and
  captures events (new tasks, human replies, permission verdicts)
  into the AgentRQ place through `/presence/turn`.
- **Declarative widget** showing live workspace connection status.
- **Zero external dependencies** — uses only Python stdlib (`urllib`,
  `json`, `socket`, `signal`).

## Setup

1. Deploy AgentRQ (Docker: `docker pull agentrq/agentrq:latest` or
   self-host via [SETUP.md](https://github.com/agentrq/agentrq/blob/main/SETUP.md)).
2. Create a workspace in the AgentRQ UI. Copy its **MCP URL** and **Token**
   from the workspace setup modal.
3. In Ouroboros **Settings → Secrets**, add two keys:
   - `AGENTRQ_MCP_URL` — the workspace MCP endpoint
     (e.g. `https://WORKSPACE_ID.mcp.agentrq.com/` or
     `http://localhost:8080/mcp/WORKSPACE_ID`)
   - `AGENTRQ_TOKEN` — the 16-char workspace token
4. Enable this skill and grant the two keys when prompted.

## Agent workflow

```
rq_workspace()             → see mission + stats
rq_pull_next()             → dequeue next task
rq_update_status(ongoing)  → mark task in progress
... do the work ...
rq_reply(task_id, result)  → post result to task thread
rq_update_status(completed)→ mark task done
```

To request human permission for a sensitive action:

```
rq_create_task(title="Approve: deploy to prod", assignee="human")
→ human sees it in AgentRQ UI, replies allow/deny
rq_get_messages(task_id) → read the verdict
```

## AgentRQ place

Connect provider `agentrq` with the workspace MCP URL (without credentials or
query parameters) as the account ID. Tasks are conversations. Assigned agent
tasks and human replies carry `direct_message`; other updates are captured
without inventing addressing. Place notes and wakes govern participation.
A places-capable Host is required; events never enter owner chat.

Notifications are persisted before submission, with their capture order.
Provider event/message IDs are preferred; when absent, the complete notification
supplies a stable digest. Host receipts and deferred references survive restart.
Automatic replies use the task's `reply` operation and a live place check:
Silent/Paused holds them. Uncertain physical delivery is recorded and never
retried automatically. Events without a task ID are heard at the workspace; a
reply without a task is held.

A task's events reach the Host in capture order: the next one goes once the Host
has answered the one before, while up to three tasks go side by side. A Host that
lists continuation version 2 in `presence_continuation_versions` is asked for it:
it answers an event `continuing` as soon as the task's author takes later events,
so the task's next event reaches that author as context. A busy conversation or
full capacity (`presence_capacity_full`) keeps the event at its task's head; the
same event goes again after the Host's `retry_after`, a floor used as it is (1 s
for a conversation, 5 s for capacity and for the older in-flight refusal), while
other tasks go on. Without that list the earlier request stays, which waits for
the author's end. Host intake and the reading of Host references
are separate loops: each reference (the turn, its continuation, each child) is
read until its own end, and each selected output is replied once by its output
identity. Failed, cancelled or still-running work speaks only through an output
identity, so a stop sends nothing. A `reply_anchor` of the same task is recorded
with its output; the reply always goes to the event's task.

When the listener stops, nothing new is submitted, read or replied; submissions
already in flight finish and their answers are kept. An event that got no answer
is offered again, unchanged, at the next start, and a reply already sending stays
`sending`, then `uncertain`, and is never sent again.
