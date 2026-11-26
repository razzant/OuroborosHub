---
name: todoist-mcp-guide
title: "Todoist MCP: tasks and projects"
description: "How to add Todoist's official MCP server to Ouroboros and sign in: find, add, edit, complete and reschedule tasks and work with projects, sections, comments, labels, filters and reminders. For Todoist users on any plan, including the free one; the sign-in asks for read and write access."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants Ouroboros to read or manage their Todoist tasks and projects, or asks how to connect Todoist's MCP server. Needs the person's own Todoist account and sign-in."
model_experience:
  what_model_sees: "A short guide: the server address, the sign-in steps under Extensions → MCP servers, the access the sign-in asks for, the tool groups and Todoist's limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server after the person signs in; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# Todoist MCP

Todoist runs an MCP server for its task manager. It is for Todoist users on any plan,
including the free Beginner plan. After the person signs in, an assistant can find and
read tasks, projects and activity, and can add, edit, complete and reschedule tasks and
work with projects, sections, comments, labels, filters and reminders.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from Todoist's server.

## Server

- Address: `https://ai.todoist.net/mcp`
- Transport: Streamable HTTP
- Sign-in: the person's Todoist account, on Todoist's own page. Ouroboros registers
  itself with Todoist automatically, so no client ID is needed.
- Access: the sign-in asks for `data:read_write`. Todoist's API documentation describes
  it as read and write access to tasks, projects, labels and filters. For apps that
  register themselves, as Ouroboros does, Todoist's consent screen can offer Full access
  or Read-only and starts on Full access. Todoist lists deleting data and deleting
  projects as separate permissions, `data:delete` and `project:delete`, which this
  sign-in does not ask for.
- Plans: Todoist's help page lists the feature for Beginner (free), Pro and Business.
  Pro costs US$5 a month billed yearly or US$7 monthly; Business US$8 per user a month
  billed yearly or US$10 monthly, plus local tax.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `Todoist`, **Server ID** `todoist`,
   **Transport** Streamable HTTP and **Address** `https://ai.todoist.net/mcp`. Leave the
   headers empty.
3. Press **Test**. Before sign-in it reports that the server needs the owner's sign-in.
4. Switch on **Use this server** at the top of the form and press **Save**. Ouroboros
   returns to the list of servers, where the server's row shows **Sign in**.
5. Open the server from the list and press **Sign in**. Ouroboros opens Todoist's
   sign-in page; **Open the sign-in page** shows the same link. Sign in and allow access.
6. When the browser comes back to Ouroboros, the server's page lists the tools. If it
   does not come back, paste the address the browser stopped on under **Details** and
   press **Finish sign-in**.

On 2026-11-26 this path was walked in a clean Ouroboros up to Todoist's login page; no
account was used.

## Tools

The source of Todoist's server is open on GitHub. Its tools:

- find tasks by text, date or completion, and find projects, sections, comments,
  labels, filters, folders, reminders, collaborators and activity;
- add tasks, projects, sections, comments (by default `add-comments` notifies task or
  thread participants in shared projects unless silenced with `["none"]`, and can notify
  named users), labels, filters, folders and reminders;
- complete, reopen, reschedule, reorder and move tasks and projects, archive, restore
  (unarchive) and join workspace projects (`project-management`), update comments, manage
  assignments, and read, set or clear the user's status shown next to their name in
  Todoist and Comms (`set-user-status`, which replaces the whole status and can also
  pause Comms notifications or Karma and streaks);
- read workspace insights, project health and productivity statistics, and export or
  import project templates.

The source also has a delete tool; deleting needs the separate permissions above. The
exact list appears on the server's page in Ouroboros after sign-in.

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
- [Todoist API authorization](https://developer.todoist.com/api/v1/authorization.md):
  permission scopes and the consent screen. Checked 2026-11-26.
- [Todoist pricing](https://www.todoist.com/pricing). Checked 2026-11-26.
- [todoist/todoist-mcp](https://github.com/todoist/todoist-mcp) (v17.4.1): tool sources.
  Checked 2026-11-26.
- Sign-in discovery, client registration, the requested permission and the login page:
  Ouroboros's Test and Sign in on 2026-11-26.
