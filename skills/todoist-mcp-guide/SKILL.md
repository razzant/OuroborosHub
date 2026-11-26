---
name: todoist-mcp-guide
title: "Todoist MCP: tasks and projects"
description: "How to add Todoist's official MCP server to Ouroboros and sign in: find, create, edit, complete and reschedule tasks, and manage projects, sections, comments, labels, filters and reminders. Available on every Todoist plan; access is read and write."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants Ouroboros to read or manage their Todoist tasks and projects, or asks how to connect Todoist's MCP server. Needs the person's own Todoist account and sign-in."
model_experience:
  what_model_sees: "A short guide: the server address, the sign-in steps under Extensions → MCP servers, the access the sign-in grants, the tool groups and Todoist's limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server after the person signs in; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# Todoist MCP

Todoist runs an MCP server for its task manager. After the person signs in with their
Todoist account, an assistant can find and read tasks, projects and activity, and can
create, edit, complete, reschedule and delete them.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from Todoist's server.

## Server

- Address: `https://ai.todoist.net/mcp`
- Transport: Streamable HTTP
- Sign-in: the person's Todoist account, on Todoist's own page. Ouroboros registers
  itself with Todoist automatically, so no client ID is needed.
- Access: the sign-in asks for one permission, `data:read_write`. Todoist describes it
  as reading and changing Todoist data: creating tasks and projects, editing what is
  there and completing tasks.
- Plans: Todoist's help page lists the feature for Beginner (free), Pro and Business.
  Pro costs US$5 a month billed yearly or US$7 monthly; Business US$8 per user a month
  billed yearly or US$10 monthly, plus local tax.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `Todoist`, **Server ID** `todoist`,
   **Transport** Streamable HTTP and **Address** `https://ai.todoist.net/mcp`. Leave the
   headers empty.
3. Press **Test**. Before sign-in it reports that the server needs the owner's sign-in.
4. Press **Save**. The server's page now shows **Sign-in needed** and a **Sign in**
   button.
5. Press **Sign in**, then sign in to Todoist and allow access on the page that opens
   (**Open the sign-in page** shows the same link). When the browser comes back,
   Ouroboros lists the tools. If it does not come back, paste the address it stopped on
   under **Details** and press **Finish sign-in**.
6. Switch on **Use this server**.

On 2026-11-26 this flow was checked up to Todoist's login page; no account was used.

## Tools

The source of Todoist's server is open on GitHub. Its tools:

- find tasks by text, date or completion, and find projects, sections, comments,
  labels, filters, folders, reminders, collaborators and activity;
- add tasks, projects, sections, comments, labels, filters, folders and reminders;
- complete, reopen, reschedule, reorder and move tasks and projects, update comments
  and manage assignments;
- delete objects;
- read workspace insights, project health and productivity statistics, and export or
  import project templates.

The exact list appears on the server's page in Ouroboros after sign-in.

## Limits

- The help page says the server can have brief interruptions, and that it cannot place
  a new project inside a workspace folder while creating it.
- The Beginner plan keeps 7 days of activity history; Pro and Business keep all of it.
- On accounts managed by an organization, its administrator may have to allow
  third-party connections first.
- Access can be reviewed or revoked in Todoist under Settings → Integrations, or with
  **Sign out** on the server's page in Ouroboros.

## Sources

- [Connect Todoist to an AI assistant](https://www.todoist.com/help/todoist/todoist-and-ai/connect-todoist-to-an-ai-assistant-xMSzFfHng)
  (updated 2026-10-01): address, permission, plans and limits. Checked 2026-11-26.
- [Todoist pricing](https://www.todoist.com/pricing). Checked 2026-11-26.
- [todoist/todoist-mcp](https://github.com/todoist/todoist-mcp): tool sources.
  Checked 2026-11-26.
- Sign-in discovery, client registration and the login page: Ouroboros's Test and
  Sign in on 2026-11-26.
