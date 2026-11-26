---
name: vk-workspace-mcp-guide
title: "VK WorkSpace MCP: messenger, mail, disk and calendar"
description: "How to add VK WorkSpace's official MCP server to Ouroboros and sign in with a company account: read and send messages and email, manage chats, mail threads and files, share files and change access, and create, change and delete calendar events. For organizations on VK WorkSpace with the paid Assistant option on the Extended or Individual plan."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person whose organization uses VK WorkSpace wants Ouroboros to read, send or manage company messages, mail, files or calendar events, or asks how to connect VK WorkSpace's MCP server. Needs a company account and the Assistant option enabled by the organization's administrator."
model_experience:
  what_model_sees: "A short guide: the server address, who can use it, the sign-in steps under Extensions → MCP servers, the 57 tools VK documents with their effects, plans and limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server after the person signs in; with 57 tools, leaving Schemas from task start off lets Ouroboros load call schemas only when needed."
---

# VK WorkSpace MCP

VK WorkSpace is VK's workplace suite for organizations: the VK Teams messenger, mail,
disk and calendar. It is for organizations on VK WorkSpace with the paid Assistant
option on the Extended or Individual plan; Extended starts at 499 ₽ per user a month.
After the person signs in with their company account, an assistant can read and send
messages and email, manage chats, mail threads and files, share files and change their
access, and create, change and delete calendar events.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from VK WorkSpace's server.

## Server

- Address: `https://ai.workspace.vk.ru/mcp`
- Transport: Streamable HTTP
- Sign-in: the person's VK WorkSpace account in the browser, through VK ID. Ouroboros
  registers itself automatically, so no client ID is needed.
- Who can use it: organizations on the Extended («Расширенный») or Individual
  («Индивидуальный») plan, after an administrator turns on and pays for the Assistant
  option in the admin panel under the plan's options.
- Prices: VK's tariffs page gives Extended from 499 ₽ per user a month, with a 14-day
  trial for 1 ₽ on the first connection, and the Individual plan by agreement. It states
  no price for the Assistant option.
- Language: the documentation is in Russian.

## Add it to Ouroboros

These steps are for Ouroboros 7.11 or later.

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `VK WorkSpace`, **Server ID**
   `vk_workspace`, **Transport** Streamable HTTP and **Address**
   `https://ai.workspace.vk.ru/mcp`. Leave the headers empty.
3. Press **Test**. Before sign-in it reports that the server needs the owner's sign-in.
4. Switch on **Use this server** at the top of the form and press **Save**. Ouroboros
   returns to the list of servers, where the server's row shows **Sign in**.
5. Open the server from the list and press **Sign in**. Ouroboros opens the VK ID
   sign-in page; **Open the sign-in page** shows the same link. Sign in with the company
   account and allow access.
6. When the browser comes back to Ouroboros, the server's page lists the tools. If it
   does not come back, paste the address the browser stopped on under **Details** and
   press **Finish sign-in**.

On 2026-11-26 the sign-in was checked up to the VK ID sign-in page; the step order was
walked in a clean Ouroboros with another sign-in server. No account was used.

## Tools

VK's user guide lists 57 tools:

- Messenger (24): list and search chats, messages and folders, find people and chats,
  read chat details, reactions and files; send messages in chats and threads, set or clear a
  chat's draft, open a thread, mark a chat as read; create, rename and delete chat
  folders (chats stay) and change which chats they hold; create group chats, add or
  remove members, join or leave groups and channels; and re-create the session and its
  chat cache.
- Mail (13): unread mail, folders, threads, search and contacts; send an email,
  schedule it, save a draft; flag messages, move threads, delete threads (to Trash) and
  unsubscribe from a mailing.
- Disk (10): list, inspect and search files; create folders and files, move items,
  remove them to Trash, create public links, see who has access, and invite people,
  change their access level, remove access or close a public link.
- Calendar (10): list events, attendees, calendars and free slots; create events,
  including recurring ones with a meeting room and a call; reschedule or edit them,
  change recurrence, cancel or delete them, and add or remove attendees.

## Limits

- Per VK, the assistant uses the user's access rights and sees only what the user can
  see.
- Only for organizations with the paid Assistant option; personal accounts are not
  described as supported.
- If the connection fails, VK suggests checking network access to
  `https://ai.workspace.vk.ru`.

## Sources

- [VK WorkSpace MCP user guide](https://workspace.vk.ru/docs/saas/user-guides/ru/vk-teams/ai/mcp)
  (updated 2026-11-24): address and the 57 tools. Checked 2026-11-26.
- [VK WorkSpace MCP setup](https://ai.workspace.vk.ru/mcp/help) and its
  [documentation copy](https://workspace.vk.ru/docs/saas/ru/vk-teams/assistant/mcp)
  (updated 2026-11-19): address, sign-in and clients. Checked 2026-11-26.
- [VK WorkSpace Assistant](https://workspace.vk.ru/docs/saas/ru/vk-teams/assistant):
  plans, the Assistant option and access rights. Checked 2026-11-26.
- [VK WorkSpace tariffs](https://workspace.vk.ru/docs/saas/ru/tariffs/info) (updated
  2026-06-24). Checked 2026-11-26.
- Sign-in discovery, client registration and the sign-in page: Ouroboros's Test and
  Sign in on 2026-11-26.
