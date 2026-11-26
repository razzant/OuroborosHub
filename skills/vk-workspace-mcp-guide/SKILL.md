---
name: vk-workspace-mcp-guide
title: "VK WorkSpace MCP: a company's workspace data"
description: "How to add VK WorkSpace's official MCP server to Ouroboros and sign in with a company account: an assistant works with the organization's VK WorkSpace data within the user's own access rights. Needs the paid Assistant option on the Extended or Individual plan."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person whose organization uses VK WorkSpace wants Ouroboros to search or use the company's workspace data, or asks how to connect VK WorkSpace's MCP server. Needs a company account and the Assistant option enabled by the organization's administrator."
model_experience:
  what_model_sees: "A short guide: the server address, who can use it, the sign-in steps under Extensions → MCP servers and the limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server after the person signs in; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# VK WorkSpace MCP

VK WorkSpace is VK's suite for organizations, with company mail, the VK Teams messenger,
calendar and files. Its MCP server connects an assistant to the organization's VK
WorkSpace data. The assistant works with the user's access rights and sees only what
the user can see.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from VK WorkSpace's server.

## Server

- Address: `https://ai.workspace.vk.ru/mcp`
- Transport: Streamable HTTP
- Sign-in: the user's VK WorkSpace account in the browser, through VK ID. Ouroboros
  registers itself automatically, so no client ID is needed.
- Who can use it: organizations on the Extended («Расширенный») or Individual
  («Индивидуальный») plan, after an administrator turns on and pays for the Assistant
  option in the admin panel under the plan's options. The vendor pages read on
  2026-11-26 do not state its price.
- Language: the documentation is in Russian.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `VK WorkSpace`, **Server ID**
   `vk_workspace`, **Transport** Streamable HTTP and **Address**
   `https://ai.workspace.vk.ru/mcp`. Leave the headers empty.
3. Press **Test**. Before sign-in it reports that the server needs the owner's sign-in.
4. Press **Save**. The server's page now shows **Sign-in needed** and a **Sign in**
   button.
5. Press **Sign in**, then sign in with the company account and allow access on the
   page that opens. When the browser comes back, Ouroboros lists the tools. If it does
   not come back, paste the address it stopped on under **Details** and press **Finish
   sign-in**.
6. Switch on **Use this server**.

On 2026-11-26 this flow was checked up to the VK ID sign-in page; no account was used.

## Tools

VK publishes no tool list. Its examples for the assistant include finding who wrote
about a topic, finding a document or a file, and looking at the calendar. The tools
appear on the server's page in Ouroboros after sign-in.

## Limits

- Only for organizations with the paid Assistant option; personal accounts are not
  described as supported.
- If the connection fails, VK suggests checking network access to
  `https://ai.workspace.vk.ru`.

## Sources

- [VK WorkSpace MCP setup](https://ai.workspace.vk.ru/mcp/help) and its
  [documentation copy](https://workspace.vk.ru/docs/saas/ru/vk-teams/assistant/mcp)
  (updated 2026-11-19): address, sign-in and clients. Checked 2026-11-26.
- [VK WorkSpace Assistant](https://workspace.vk.ru/docs/saas/ru/vk-teams/assistant):
  plans, the Assistant option and access rights. Checked 2026-11-26.
- Sign-in discovery, client registration and the sign-in page: Ouroboros's Test and
  Sign in on 2026-11-26.
