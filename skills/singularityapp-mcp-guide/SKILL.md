---
name: singularityapp-mcp-guide
title: "SingularityApp MCP: tasks, projects and habits"
description: "How to add SingularityApp's official MCP server to Ouroboros and sign in: read, create and edit tasks, checklists, projects, habits, tags and Kanban boards, with an optional batch mode. MCP is marked for the Pro and Elite plans."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants Ouroboros to read or manage their SingularityApp tasks, projects, habits or Kanban boards, or asks how to connect SingularityApp's MCP server. Needs the person's SingularityApp account and sign-in."
model_experience:
  what_model_sees: "A short guide: the server address and its toolsets option, the sign-in steps under Extensions → MCP servers, the tool groups, plans and limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server after the person signs in; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# SingularityApp MCP

SingularityApp is a planner for tasks, projects and habits. Its MCP server lets an
assistant read, create and edit tasks and checklists, projects, habits, tags and Kanban
boards in the person's account after sign-in.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from SingularityApp's server.

## Server

- Address: `https://mcp.singularity-app.com/mcp`
- Tool groups: by default the server offers its main tools. The `toolsets` parameter in
  the address picks groups, for example
  `https://mcp.singularity-app.com/mcp?toolsets=tasks,projects`. The groups are `tasks`,
  `projects`, `meta` (core entities), `habits`, `kanban`, `tags` and `system`, which is
  batch mode: off by default, up to 20 operations in one request when included.
- Transport: Streamable HTTP
- Sign-in: the person's SingularityApp account, on SingularityApp's own page.
  Ouroboros registers itself automatically. The sign-in asks for read and write access
  to tasks, checklists, projects, habits, tags, Kanban boards and time statistics.
- Plans: the vendor's MCP page marks MCP as Pro and Elite. Pro costs US$3.99 a month,
  or US$3.33 a month billed annually; Elite US$4.99 a month, or US$4.17 billed
  annually. The free Basic plan has up to 10 projects and 3 habits.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `SingularityApp`, **Server ID**
   `singularityapp`, **Transport** Streamable HTTP and **Address**
   `https://mcp.singularity-app.com/mcp`, with `?toolsets=...` added if you want specific
   groups. Leave the headers empty.
3. Press **Test**. Before sign-in it reports that the server needs the owner's sign-in.
4. Press **Save**. The server's page now shows **Sign-in needed** and a **Sign in**
   button.
5. Press **Sign in**, then sign in to SingularityApp and allow access on the page that
   opens. When the browser comes back, Ouroboros lists the tools. If it does not come
   back, paste the address it stopped on under **Details** and press **Finish sign-in**.
6. Switch on **Use this server**.

On 2026-11-26 this flow was checked up to SingularityApp's sign-in page, both with the
plain address and with `?toolsets=tasks,projects`; no account was used.

## Tools

By group, per the vendor:

- `tasks`: read tasks and checklists, create and edit tasks, change completion.
- `projects`: read, create and edit projects.
- `meta`: create and edit the app's core entities.
- `habits`: read, create and edit habits.
- `kanban`: read, create and edit boards.
- `tags`: read, create and edit tags.
- `system`: batch mode for several operations in one request.

The exact list appears on the server's page in Ouroboros after sign-in.

## Limits

- Without batch mode, many changes run one by one.
- Connected apps can be reviewed and revoked in the SingularityApp account under
  [connected apps](https://me.singularity-app.com/oauth-tokens).
- The vendor states no usage limits.
- The sign-in page offers English and Russian.

## Sources

- [SingularityApp MCP](https://singularity-app.com/wiki/mcp/): address, toolsets,
  sign-in and the Pro and Elite marks. Checked 2026-11-26.
- [SingularityApp plans](https://singularity-app.com/price/). Checked 2026-11-26.
- Sign-in discovery, client registration and the sign-in page: Ouroboros's Test and
  Sign in on 2026-11-26.
