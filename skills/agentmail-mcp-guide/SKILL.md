---
name: agentmail-mcp-guide
title: "AgentMail MCP: email inboxes for the agent"
description: "How to add AgentMail's official MCP server to Ouroboros: give the agent its own email inboxes to send, receive, reply and forward mail, keep drafts, a calendar and files, and sign up to apps with an inbox. For anyone who wants the agent to have its own address; a free plan with 3 inboxes, sign-in or API key."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants Ouroboros to have its own email address for sending and receiving mail, or asks to connect AgentMail's MCP server. AgentMail inboxes belong to the AgentMail account; this is not access to the person's existing mailbox."
model_experience:
  what_model_sees: "A short guide: the server address, the two ways to connect under Extensions → MCP servers, the tools the server listed on 2026-11-26 with their regional and Labs conditions, plans and limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; with 54 tools, leaving Schemas from task start off lets Ouroboros load call schemas only when needed."
---

# AgentMail MCP

AgentMail provides email inboxes for AI agents. It is for anyone who wants the agent to
have its own address, and it has a free plan with three inboxes. Through its MCP server
an assistant can create inboxes, send, receive, reply to and forward mail, keep drafts
and scheduled sends, manage a calendar and store files for an inbox, and use an inbox as
its identity to sign up to other apps. An inbox is the agent's own address in the
AgentMail account, not the person's existing mailbox.

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
- Plans: AgentMail's pricing page and its rate-limits page agree that the Free plan
  has 3 inboxes and 100 emails a day. The pricing page also lists 3,000 emails a month,
  3 GB of storage and one custom domain for Free; the rate-limits page says Free has no
  monthly cap and no custom domain. Paid plans: Developer US$20 a month (10 inboxes,
  10,000 emails a month), Startup US$200 a month (150 inboxes), Enterprise by agreement.
  The pricing page lists the MCP server on every plan.

## Add it to Ouroboros

These steps are for Ouroboros 7.11 or later.

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `AgentMail`, **Server ID** `agentmail`,
   **Transport** Streamable HTTP and **Address** `https://mcp.agentmail.to/mcp`.
3. Then choose one way:
   - **With sign-in:** leave the headers empty and press **Test**; before sign-in it
     reports that the server needs the owner's sign-in. Switch on **Use this server** at
     the top of the form and press **Save**. Ouroboros returns to the list of servers,
     where the server's row shows **Sign in**. Open the server and press **Sign in**;
     Ouroboros opens the AgentMail console's sign-in page (**Open the sign-in page** shows
     the same link). Sign in and allow access. When the browser comes back, the server's
     page lists the tools. If it does not come back, paste the address the browser stopped
     on under **Details** and press **Finish sign-in**.
   - **With an API key:** open **More: headers, tools, schemas**, press **Add header**,
     enter **Header name** `x-api-key` and the key as **Header value**, and press **Test**.
     Then switch on **Use this server** and press **Save**.

On 2026-11-26 Test without credentials reported that the server needs sign-in, Sign in
was checked up to the AgentMail console's sign-in page, and Test with a placeholder
`x-api-key` value made the server list its 54 tools. No account or real key was used,
and no tool was called.

## Tools

The server listed 54 tools on 2026-11-26:

- Inboxes: list, search, get, create, update and delete. Deleting an inbox permanently
  removes its threads, messages, drafts, calendar and calendar events; a new inbox
  created later with the same address starts with an empty calendar.
- Mail: list, search and read threads and messages, change labels, delete threads, and
  read attachments.
- Sending: `send_message`, `reply_to_message`, `forward_message` and `send_draft` send
  at once; the server describes sent mail as impossible to recall.
- Drafts: create (optionally with a scheduled send time), update, send and delete.
- Calendar: agenda and events, create, update and delete events with optional
  invitations, answer invitations, and the calendar's default time zone. Per the tool
  descriptions, the calendar works in AgentMail's US production; EU production answers
  403.
- Agent Drive: upload, list, get and delete an inbox's files. Per the tool descriptions,
  Agent Drive is in AgentMail Labs, US only; an organization outside Labs gets 403.
- Allow and block lists: list, read, add (`create_list_entry`) and remove
  (`delete_list_entry`) email addresses or domains on an inbox's `send`, `receive` or
  `reply` lists. Adding the first allow entry for a direction turns that allow list on,
  excluding every other sender or recipient for that direction; only the inbox's own
  non-read-only entries can be removed.
- Account: attach a human to a new agent organization and verify it with an emailed
  code; AgentMail Labs (reading needs organization-level access, and joining needs an
  organization API key without a permissions restriction); listing and selecting
  organizations, which works only with sign-in, not with an API key; and feedback to
  AgentMail.
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
- [AgentMail pricing](https://www.agentmail.to/pricing) and
  [rate limits](https://docs.agentmail.to/knowledge-base/rate-limits). Checked 2026-11-26.
- [AgentMail calendar](https://docs.agentmail.to/calendar): regions and inbox deletion.
  Checked 2026-11-26.
- Sign-in discovery, client registration, the sign-in page and the tool list with its
  descriptions: Ouroboros's Test and Sign in on 2026-11-26.
