---
name: readwise-mcp-guide
title: "Readwise MCP: highlights and Reader library"
description: "How to add Readwise's official MCP server to Ouroboros and sign in: search and read your highlights and Reader documents, get the Daily Review, save documents, tag, move, annotate and edit highlights. A paid Readwise plan after a 30-day trial."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants Ouroboros to search or use their Readwise highlights or Reader library, save something to Reader, or asks how to connect Readwise's MCP server. Needs the person's Readwise account and sign-in."
model_experience:
  what_model_sees: "A short guide: the server address, the sign-in steps under Extensions → MCP servers, the tools Readwise documents, plans and limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server after the person signs in; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# Readwise MCP

Readwise collects highlights from books and articles, and Reader is its reading app.
One MCP server covers both. After the person signs in, an assistant can search and read
highlights and Reader documents and can save, tag, move and annotate them.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from Readwise's server.

## Server

- Address: `https://mcp2.readwise.io/mcp`. Readwise says its earlier Readwise-only MCP
  server is being deprecated in favour of this one.
- Transport: Streamable HTTP
- Sign-in: the person's Readwise account, on Readwise's own page. Ouroboros registers
  itself with Readwise automatically; the sign-in asks for `openid read write`.
- Plans: Readwise is free for the first 30 days. After that, billed yearly: Full
  Readwise US$9.99 a month, Highlights only US$7.99 a month, Reader only US$7.99 a
  month. The pricing page names the MCP server for these three plans; the cheaper
  Highlights Lite plan (US$5.59 a month) does not list it.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `Readwise`, **Server ID** `readwise`,
   **Transport** Streamable HTTP and **Address** `https://mcp2.readwise.io/mcp`. Leave
   the headers empty.
3. Press **Test**. Before sign-in it reports that the server needs the owner's sign-in.
4. Press **Save**. The server's page now shows **Sign-in needed** and a **Sign in**
   button.
5. Press **Sign in**, then sign in to Readwise and allow access on the page that opens.
   When the browser comes back, Ouroboros lists the tools. If it does not come back,
   paste the address it stopped on under **Details** and press **Finish sign-in**.
6. Switch on **Use this server**.

On 2026-11-26 this flow was checked up to Readwise's login page; no account was used.

## Tools

Readwise documents these tools:

- Reader, reading: search, list and export documents, document details and metadata,
  the highlights of a document, and tags.
- Reader, changes: create a document or a highlight, add a highlight note, add or remove
  tags on documents and highlights, bulk edit document metadata, and move documents
  between the inbox, archive and shortlist.
- Readwise highlights: today's Daily Review, list and search highlights, and create,
  update or delete a highlight.

The exact list appears on the server's page in Ouroboros after sign-in.

## Limits

- The index updates as content is added, per Readwise.
- The same server address can be used in several apps at once.
- Readwise states no usage limits.

## Sources

- [Readwise MCP](https://docs.readwise.io/tools/mcp): address, sign-in, tools and
  clients. Checked 2026-11-26.
- [Readwise pricing](https://readwise.io/pricing). Checked 2026-11-26.
- Sign-in discovery, client registration and the login page: Ouroboros's Test and
  Sign in on 2026-11-26.
