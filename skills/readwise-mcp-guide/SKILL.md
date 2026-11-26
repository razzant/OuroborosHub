---
name: readwise-mcp-guide
title: "Readwise MCP: highlights, Reader and Books"
description: "How to add Readwise's official MCP server to Ouroboros and sign in: search and read highlights and Reader documents, save, tag, move and annotate them, and manage a Readwise Books library and reading sessions. For Readwise subscribers: free for 30 days, then from US$7.99 a month billed yearly for one part or US$9.99 for both."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants Ouroboros to search or use their Readwise highlights, Reader library or Readwise Books, save something to Reader, or asks how to connect Readwise's MCP server. Needs the person's Readwise account and sign-in."
model_experience:
  what_model_sees: "A short guide: the server address, the sign-in steps under Extensions → MCP servers, the tools Readwise publishes, plans and limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server after the person signs in; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# Readwise MCP

Readwise collects highlights from books and articles, Reader is its reading app, and
Readwise Books keeps a reading library and sessions. One MCP server covers all three.
It is for Readwise subscribers: Readwise is free for 30 days, then a paid plan. After
the person signs in, an assistant can search and read highlights and Reader documents,
save, tag, move and annotate them, and manage books and reading sessions.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from Readwise's server.

## Server

- Address: `https://mcp2.readwise.io/mcp`, the address on both
  [readwise.io/mcp](https://readwise.io/mcp) and Readwise's MCP documentation. Readwise
  says its earlier Readwise-only MCP server is being deprecated in favour of this one.
- Transport: Streamable HTTP
- Sign-in: the person's Readwise account, on Readwise's own page. Ouroboros registers
  itself with Readwise automatically; the sign-in asks for `openid read write`.
- Plans: Readwise is free for the first 30 days. Full Readwise includes Reader and
  Highlights together: US$9.99 a month billed yearly or US$12.99 monthly. Highlights
  only and Reader only cost US$7.99 a month billed yearly or US$8.99 monthly; the
  pricing page offers the MCP server on them for highlights or for the Reader library
  respectively. Highlights Lite only (US$5.59 a month billed yearly, US$6.99 monthly)
  does not list the MCP server. The pricing page does not say which plans include the
  Books tools.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `Readwise`, **Server ID** `readwise`,
   **Transport** Streamable HTTP and **Address** `https://mcp2.readwise.io/mcp`. Leave
   the headers empty.
3. Press **Test**. Before sign-in it reports that the server needs the owner's sign-in.
4. Switch on **Use this server** at the top of the form and press **Save**. Ouroboros
   returns to the list of servers, where the server's row shows **Sign in**.
5. Open the server from the list and press **Sign in**. Ouroboros opens Readwise's
   sign-in page; **Open the sign-in page** shows the same link. Sign in and allow access.
6. When the browser comes back to Ouroboros, the server's page lists the tools. If it
   does not come back, paste the address the browser stopped on under **Details** and
   press **Finish sign-in**.

On 2026-11-26 the sign-in was checked up to Readwise's login page; the step order was
walked in a clean Ouroboros with another sign-in server. No account was used.

## Tools

[readwise.io/mcp](https://readwise.io/mcp) lists 35 tools:

- Readwise highlights (6): search and list highlights, today's Daily Review, and create,
  update or delete highlights.
- Reader documents (8): search, list and read documents and their details, create a
  document, move documents between the inbox, archive and shortlist, bulk edit document
  metadata, and export documents.
- Reader tags (3): list tags and add or remove a document's tags.
- Reader highlights (5): a document's highlights, create a highlight, add or remove a
  highlight's tags, and set a highlight's note.
- Readwise Books (13): list, add (up to 10 at a time), update and remove books; list,
  log (optionally posting to the feed), re-caption and delete reading sessions, where
  deleting also removes their feed posts; daily reading totals and yearly statistics;
  and create, list or revoke tokens that let a site read the person's public book feed.

Readwise's documentation page lists the Reader and highlights tools only. The exact list
appears on the server's page in Ouroboros after sign-in.

## Limits

- The index updates as content is added, per Readwise.
- The same server address can be used in several apps at once.
- Readwise states no usage limits.

## Sources

- [Readwise MCP](https://readwise.io/mcp): address and the 35 tools. Checked 2026-11-26.
- [Readwise MCP documentation](https://docs.readwise.io/tools/mcp): address, sign-in and
  clients. Checked 2026-11-26.
- [Readwise pricing](https://readwise.io/pricing), yearly and monthly views. Checked
  2026-11-26.
- Sign-in discovery, client registration and the login page: Ouroboros's Test and
  Sign in on 2026-11-26.
