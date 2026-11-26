---
name: pecom-mcp-guide
title: "PEK MCP: freight cost, cargo tracking and branches"
description: "How to add PEK's official MCP server to Ouroboros: estimate the cost and transit time of a freight shipment in Russia, check a cargo's status by its number and find the nearest PEK branches. For anyone shipping with PEK; free per PEK, no account, read-only."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants to estimate a PEK freight delivery, check a cargo's status by its tracking number or find a PEK branch, or asks to connect PEK's MCP server. Not for placing an order: ordering and payment stay on the PEK website."
model_experience:
  what_model_sees: "A short guide: the server address, how to add it under Extensions → MCP servers, the tools the server listed on 2026-11-26 beside the names on PEK's page, the connection record and limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# PEK MCP

PEK (ПЭК) is a Russian freight carrier. Its public MCP server is for anyone shipping
with PEK: per PEK it is free, and it needs no account. An assistant can estimate the
cost and transit time of a shipment by route, weight and size, check a cargo's status
by its tracking number and find the nearest PEK branches with their opening hours. The
server only reads data. Per PEK, the assistant can prepare a link to PEK's order form
with a summary of the parameters; the person places the order and pays on the PEK
website.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from PEK's server.

## Server

- Address: `https://pecom.ru/mcp`
- Transport: Streamable HTTP
- Sign-in: none, and no PEK account is needed.
- Price: free, according to PEK.
- Coverage and language: PEK's network in Russia; the vendor page is in Russian.
- Connection record: on 2026-11-26, 1 of 7 connection attempts from one network
  succeeded; the others timed out.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `PEK`, **Server ID** `pek`, **Transport**
   Streamable HTTP and **Address** `https://pecom.ru/mcp`. Leave the headers empty.
3. Press **Test**. The successful Test on 2026-11-26 listed 3 tools. If Test times out,
   try again later.
4. Switch on **Use this server** and press **Save**.

## Tools

The server listed three tools on 2026-11-26; PEK's page gives them other names:

| Listed by the server | Named on PEK's page | Purpose |
|---|---|---|
| `calculate_price` | `pecom_calculate_price` | cost and transit time for a route, weight and size |
| `get_cargo_status` | `pecom_cargo_status` | a cargo's status by its tracking number |
| `get_nearest_department` | `pecom_departments_search` | the nearest PEK branches and their opening hours |

## Limits

- Read-only: per PEK, the assistant changes nothing in PEK's systems, and ordering and
  payment stay on the PEK website.
- The price is preliminary; PEK sets the exact cost when the order is placed.
- PEK's API allows 100 requests a minute. Above that the server answers with a request
  to retry in a given number of seconds.
- Per PEK, the AI sees only what the person wrote in the request and PEK's answer, not a
  personal account, documents or recipients' data.

## Sources

- [PEK MCP](https://pecom.ru/pecom-mcp/): address, transport, free read-only access,
  limits and the tool names on the page. Checked 2026-11-26.
- Connection attempts and the tool list: Ouroboros's Test on 2026-11-26.
