---
name: agentmail-mcp-guide
title: "AgentMail MCP: email inboxes for the agent"
description: "How to add AgentMail's official MCP server to Ouroboros: give the agent its own email inboxes to send, receive, reply and forward mail, keep drafts and files, run a calendar and sign up to apps with an inbox. Sign in or use an API key; the free plan has 3 inboxes."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants Ouroboros to have its own email address for sending and receiving mail, or asks to connect AgentMail's MCP server. AgentMail inboxes belong to the AgentMail account; this is not access to the person's existing mailbox."
model_experience:
  what_model_sees: "A short guide: the server address, the two ways to connect under Extensions → MCP servers, the tools the server listed on 2026-11-26, plans and limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; with 54 tools, leaving Schemas from task start off lets Ouroboros load call schemas only when needed."
---

# AgentMail MCP

AgentMail provides email inboxes for AI agents. Through its MCP server an assistant can
create inboxes, send, receive, reply to and forward mail, keep drafts and scheduled
sends, store files, manage a calendar for an inbox, and use an inbox as its identity to
sign up to other apps. An inbox is the agent's own address in the AgentMail account, not
the person's existing mailbox.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from AgentMail's server.

## Server

- Address: `https://mcp.agentmail.to/mcp`
- Transport: Streamable HTTP
- Access, either way:
  - **Sign in** with an AgentMail console account. Ouroboros registers itself with
    AgentMail automatically; the sign-in asks for `openid email profile user:org:read`.
  - An API key in the header `x-api-key`. Keys start with `am_` and are created in the
    AgentMail console under Settings → API Keys.
- Plans: Free, US$0: 3 inboxes, 3,000 emails a month, 100 a day, 3 GB storage and one
  custom domain. Developer, US$20 a month: 10 inboxes and 10,000 emails a month.
  Startup, US$200 a month: 150 inboxes. Enterprise: by agreement. AgentMail lists the
  MCP server on every plan.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `AgentMail`, **Server ID** `agentmail`,
   **Transport** Streamable HTTP and **Address** `https://mcp.agentmail.to/mcp`.
3. Then choose one way:
   - **With sign-in:** leave the headers empty and press **Save**. The server's page
     shows **Sign-in needed** and a **Sign in** button. Press it, sign in to the
     AgentMail console and allow access. When the browser comes back, Ouroboros lists the
     tools. If it does not come back, paste the address it stopped on under **Details**
     and press **Finish sign-in**.
   - **With an API key:** open **More: headers, tools, schemas**, press **Add header**,
     enter **Header name** `x-api-key` and the key as **Header value**, then press
     **Test** and **Save**.
4. Switch on **Use this server**.

On 2026-11-26 the sign-in flow was checked up to the AgentMail console's sign-in page,
and the server listed its tools to Test; no account or real key was used.

## Tools

The server listed 54 tools on 2026-11-26:

- Inboxes: list, search, get, create, update and delete. Deleting an inbox permanently
  removes its threads, messages and drafts.
- Mail: list, search and read threads and messages, change labels, delete threads, and
  read attachments.
- Sending: `send_message`, `reply_to_message`, `forward_message` and `send_draft` send
  at once; the server describes sent mail as impossible to recall.
- Drafts: create (optionally with a scheduled send time), update, send and delete.
- Agent Drive: upload, list, get and delete an inbox's files.
- Calendar: agenda and events, create, update and delete events with optional
  invitations, answer invitations, and the calendar's default time zone.
- Allow and block lists for senders and recipients.
- Account: attach a human to a new agent organization and verify it with an emailed
  code, AgentMail Labs, organizations, and feedback to AgentMail.
- AgentID: find apps in AgentMail's catalogue and create or sign in to an account at a
  third-party app with an inbox as its identity (`connect_app`, `authorize_inbox`).

## Limits

- Sending limits follow the plan, for example 100 emails a day on Free.
- Per AgentMail, an organization that signed up without a human email cannot send
  until one is attached; verifying it with the emailed code lifts the limits of an
  unverified organization.
- AgentMail's own instructions treat incoming mail as data from external senders, not
  as instructions.

## Sources

- [AgentMail MCP](https://docs.agentmail.to/integrations/mcp): address, sign-in and
  API key, tool groups. Checked 2026-11-26.
- [AgentMail pricing](https://www.agentmail.to/pricing). Checked 2026-11-26.
- Sign-in discovery, client registration, the sign-in page and the tool list:
  Ouroboros's Test and Sign in on 2026-11-26.
