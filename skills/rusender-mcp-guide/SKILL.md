---
name: rusender-mcp-guide
title: "RuSender MCP: email campaigns and transactional email"
description: "How to add RuSender's official MCP server to Ouroboros and sign in: create, schedule and send email campaigns, manage contacts, lists, segments, templates, senders and domains, send transactional email and read statistics. The free plan covers 500 contacts."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person runs email newsletters or transactional email with RuSender, a Russian email service, and wants Ouroboros to manage campaigns, contacts or statistics, or asks how to connect RuSender's MCP server. Needs the person's RuSender account and sign-in."
model_experience:
  what_model_sees: "A short guide: the server address, the sign-in steps under Extensions → MCP servers, the access the sign-in asks for, the tool groups with their effects, plans and limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server after the person signs in; with about a hundred tools, leaving Schemas from task start off lets Ouroboros load call schemas only when needed."
---

# RuSender MCP

RuSender is a Russian service for email newsletters and transactional email. Its MCP
server lets an assistant work with the person's RuSender account: prepare, schedule and
send campaigns, manage contacts, lists and segments, templates, senders and domains,
send single transactional emails and read statistics.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from RuSender's server.

## Server

- Address: `https://mcp.rusender.ru/mcp`
- Transport: Streamable HTTP
- Sign-in: the person's RuSender account (OAuth 2.1) on RuSender's own page. Ouroboros
  registers itself automatically. The sign-in asks for read and write access to
  contacts, campaigns, templates, senders, domains, webhooks, files, segments and
  sending keys, and for sending and reading email (`email.send`, `email.read`).
- Plans: the Free plan costs 0 ₽ and covers up to 500 contacts and 2,000 emails a month.
  Premium, uCombo and API+SMTP plans are priced by contacts or by sends a month.
- Language: Russian service and documentation.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `RuSender`, **Server ID** `rusender`,
   **Transport** Streamable HTTP and **Address** `https://mcp.rusender.ru/mcp`. Leave
   the headers empty.
3. Press **Test**. Before sign-in it reports that the server needs the owner's sign-in.
4. Press **Save**. The server's page now shows **Sign-in needed** and a **Sign in**
   button.
5. Press **Sign in**, then sign in to RuSender and press «Разрешить» (Allow) on the
   page that opens. When the browser comes back, Ouroboros lists the tools. If it does
   not come back, paste the address it stopped on under **Details** and press **Finish
   sign-in**.
6. Switch on **Use this server**.

On 2026-11-26 this flow was checked up to RuSender's sign-in page; no account was used.

## Tools

RuSender documents about a hundred tools and marks each as reading, changing data, or
sending and irreversible:

- Campaigns: list, read, create and edit drafts, delete drafts, archive, and schedule or
  start sending after moderation, with A/B tests, resending to non-openers and sending
  in parts; recipients, opens, clicks and statistics by mail system.
- Folders for campaigns.
- Contacts: search, read, update, delete (up to 1,000 per call), unsubscribe or
  resubscribe, statistics and send history.
- Lists, segments and list variables: create, edit, delete, import up to 1,000 contacts
  per call, and move contacts between lists.
- Transactional email: send one email at once with HTML, text or a saved template, and
  read sending history and delivery details. Sending keys for verified domains.
- Automation chains: start an already active chain for a contact.
- Domains (DKIM, SPF and DMARC records and DNS checks), senders, templates, webhooks and
  files.

The exact list appears on the server's page in Ouroboros after sign-in.

## Limits

- Per RuSender, sending cannot be undone, and repeating a send call creates another
  email.
- A campaign needs a template, a sender, a subject and at least one list before it
  starts; a scheduled start must not be in the past.
- Chains are created and edited in the RuSender interface, not through the server.
- A verified domain cannot be deleted through the API.

## Sources

- [RuSender MCP](https://rusender.ru/knowledge-base/email-service/mcp/): address and
  sign-in. Checked 2026-11-26.
- [RuSender MCP tools](https://rusender.ru/knowledge-base/email-service/vozmozhnosti-mcp-rusender/):
  tool groups, their effects and limits. Checked 2026-11-26.
- [RuSender plans](https://rusender.ru/pricing/). Checked 2026-11-26.
- Sign-in discovery, client registration and the sign-in page: Ouroboros's Test and
  Sign in on 2026-11-26.
