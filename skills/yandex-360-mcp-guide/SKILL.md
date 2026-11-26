---
name: yandex-360-mcp-guide
title: "Yandex 360 MCP: Mail, Calendar, Disk, Tracker and more"
description: "How to add Yandex 360's official MCP servers to Ouroboros with an OAuth token: Mail, Calendar, Disk, Telemost and organization management, plus Wiki, Forms and Tracker for organizations. Needs a paid Yandex 360 subscription; preview."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person with Yandex 360 wants Ouroboros to work with their Yandex Mail, Calendar, Disk, Telemost, Wiki, Forms, Tracker or organization settings, or asks how to connect Yandex 360's MCP servers. Needs a paid Yandex 360 subscription or Yandex 360 for Business."
model_experience:
  what_model_sees: "A short guide: one address per Yandex service, how to get a token and add it as a header under Extensions → MCP servers, what each server can read and change, who can use them and their limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the servers the person adds; the Wiki and Tracker servers list 36 and 73 tools, so leaving Schemas from task start off lets Ouroboros load call schemas only when needed."
---

# Yandex 360 MCP

Yandex runs MCP servers for its Yandex 360 services, one server per service. They act
with the rights of the person whose token is used. Yandex calls them a preview:
features, interfaces and terms may change and break compatibility.

This entry is a guide and adds no tools by itself. Add each server you need as
described below; the tools then come from Yandex's servers.

## Who can use them

- Mail, Calendar, Disk, Telemost and organization management: a Yandex ID with a paid
  Yandex 360 subscription, or an employee of an organization on Yandex 360 for Business.
  Yandex says the plan does not matter, and the servers are on by default for
  organizations; an administrator can ask Yandex support to turn them off.
- Wiki, Forms and Tracker: organizations on Yandex 360 for Business or Yandex Cloud.
- Yandex states no separate price for the servers.

## Servers

All use Streamable HTTP and a token in the `Authorization` header.

| Service | Address | Extra header |
|---|---|---|
| Mail | `https://mcp.360.yandex.net/v1/mail/mcp` | none |
| Calendar | `https://mcp.360.yandex.net/v1/calendar/mcp` | none |
| Disk | `https://mcp.360.yandex.net/v1/disk/mcp` | none |
| Telemost | `https://mcp.360.yandex.net/v1/telemost/mcp` | none |
| Organization management | `https://mcp.360.yandex.net/v1/admin/mcp` | none |
| Wiki | `https://mcp.wiki.yandex.net` | `X-Org-Id` or `X-Cloud-Org-Id` |
| Forms | `https://mcp.forms.yandex.net` | `X-Org-Id` or `X-Cloud-Org-Id` |
| Tracker | `https://mcp.tracker.yandex.net/mcp` | `X-Org-ID` or `X-Cloud-Org-ID` |

## Tokens

- Mail, Calendar, Disk, Telemost and organization management: each service's page in
  Yandex's MCP documentation has a link that issues a token for that service on
  oauth.yandex.ru. Review the access shown there, then copy the token. The header value
  is `OAuth <token>`.
- Wiki, Forms and Tracker: an OAuth token for that service's API, sent as
  `OAuth <token>`. A Yandex Cloud organization can use an IAM token as `Bearer <token>`
  instead; it lasts up to 12 hours. Add the organization ID in the extra header:
  `X-Org-Id` (`X-Org-ID` for Tracker) for Yandex 360 for Business, or `X-Cloud-Org-Id`
  (`X-Cloud-Org-ID` for Tracker) for Yandex Cloud. The ID is shown in Tracker under
  Administration → Organizations.

## Add a server to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** (for example `Yandex Disk`), **Server ID**
   (for example `yandex_disk`), **Transport** Streamable HTTP and the **Address** from
   the table.
3. Open **More: headers, tools, schemas**, press **Add header** and enter **Header
   name** `Authorization` with **Header value** `OAuth <token>`. For Wiki, Forms and
   Tracker press **Add header** again for the organization ID.
4. Press **Test**, then **Save**, and switch on **Use this server**.
5. Repeat for each service you want. Ouroboros keeps saved header values masked.

These servers have no sign-in page of their own; the token is the access. On 2026-11-26
the Mail, Calendar, Disk, Telemost and organization servers answered Test with HTTP 401
without a valid token, and the Wiki, Forms and Tracker servers listed their tools to
Test before any token; no real token was used.

## What each server can do

- **Mail:** read folders, labels and messages; create and replace drafts; move,
  label and delete messages (to Trash). Sending mail is not supported.
- **Calendar:** read events, participants, calendars, free time, offices and rooms;
  create, update, move and delete events, add or remove participants and answer
  invitations.
- **Disk:** search and list files, recent items, shared disks, photos and Trash, and get
  download links; create folders and files, copy, move, delete (to Trash), create
  public links and change their access.
- **Telemost:** read chats, members, messages and threads, find users and get meeting
  join details; create and update meetings and their co-hosts. Writing in chats is not
  supported.
- **Organization management:** read organizations, users, groups and departments;
  create domain users, update users, create, update and delete groups and departments,
  and change group members. It acts with the token owner's administrator rights.
- **Wiki** (36 tools listed on 2026-11-26): search, read and list pages; create, edit,
  append to, move and delete pages; work with tables (grids), attachments and comments.
  Yandex's `wiki:write` access covers creating, changing and deleting content.
- **Forms** (18 tools): list and read forms and questions; create and change forms,
  questions and display conditions; publish or unpublish; fill in a form; read answers,
  up to 500 per call.
- **Tracker** (73 tools): search and read issues, history and comments; create, update
  and move issues through their workflow; comments, worklogs, links, attachments,
  boards and sprints, projects, portfolios and goals, queue triggers and bulk changes.
  Some tools delete permanently, and bulk changes cannot be undone.

## Limits

- Yandex marks the servers as a preview whose interfaces may change.
- Tracker hides issues of queues whose "Access for AI tools" setting is Forbid; queue
  forms are read-only; files uploaded through the server may be up to 256 KiB, and
  download links last 5 minutes.
- Wiki works only if the Wiki administrator has not turned off API access.
- Access given to the Mail, Calendar, Disk, Telemost and organization servers can be
  revoked at id.yandex.ru under data access.

## Sources

- [Yandex 360 MCP servers](https://yandex.ru/dev/yandex-360/mcp/ru/) with the pages
  for each service: addresses, tokens, tools, who can use them and the preview notice.
  Checked 2026-11-26.
- [Wiki MCP](https://yandex.ru/support/wiki/ru/mcp),
  [Forms MCP](https://yandex.ru/support/forms/ru/mcp) and
  [Tracker MCP](https://yandex.ru/support/tracker/ru/user/mcp-server). Checked 2026-11-26.
- [Yandex news, 20 November 2026](https://yandex.ru/company/news/20-11-2026-02).
- Server answers and tool counts: Ouroboros's Test on 2026-11-26.
