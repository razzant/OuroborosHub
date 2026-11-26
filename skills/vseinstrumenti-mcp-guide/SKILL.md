---
name: vseinstrumenti-mcp-guide
title: "VseInstrumenti MCP: tools and building supplies"
description: "How to add VseInstrumenti.ru's official MCP server to Ouroboros: search, compare and find alternatives among tools, machines and supplies for building, repair and DIY, with reviews, prices and availability for Moscow (city). For buyers of tools and building supplies; no account, the vendor names no fee, and no ordering."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants tools, machines, consumables or building materials found, compared or replaced with alternatives at VseInstrumenti.ru, a Russian retailer, or asks to connect its MCP server. Not for ordering or payment, and not for goods outside building, repair and DIY."
model_experience:
  what_model_sees: "A short guide: the server address, how to add it under Extensions → MCP servers, the tools the server listed on 2026-11-26 and its limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# VseInstrumenti MCP

VseInstrumenti.ru is a Russian retailer of tools and building supplies. Its public MCP
server is for buyers of tools and supplies: it needs no account, and the vendor names no
fee. An assistant can search the catalogue, compare products, find alternatives and read
reviews. It helps choose; it does not place orders or take payment.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from VseInstrumenti's server.

## Server

- Address: `https://mcp.vseinstrumenti.ru/mcp`
- Transport: Streamable HTTP
- Sign-in: none. The vendor page says it works without authorization.
- Price: the vendor page names no fee.
- Coverage and language: prices and availability for Moscow (city), in rubles; the
  region cannot be changed through the tools. Product data and tool texts are in Russian.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `VseInstrumenti`, **Server ID**
   `vseinstrumenti`, **Transport** Streamable HTTP and **Address**
   `https://mcp.vseinstrumenti.ru/mcp`. Leave the headers empty.
3. Press **Test**. On 2026-11-26 it reported a working connection with 6 tools.
4. Switch on **Use this server** and press **Save**.

## Tools

The server listed six tools on 2026-11-26:

- `submit_request_summary`: the server asks for it once per session, before the first
  catalogue call. It takes a summary of the person's request of up to 120 characters,
  records it on the vendor's side and returns the current usage rules. The rules ask not
  to include names, contacts, addresses or other personal data in the summary.
- `search_products`: products by text query, up to 20 per page, with prices,
  availability, ratings and key characteristics.
- `get_product` and `get_products`: one or several product cards.
- `search_product_analogs`: alternatives to a product.
- `get_product_reviews`: customer reviews.

## Limits

- Search and choice only: per the vendor, the server does not place orders or take
  payment. The vendor asks to check price and availability on the product page before
  buying.
- The server's rules limit it to goods for building, repair and DIY: tools, machines,
  consumables, building and finishing materials and related goods.
- The vendor page states no usage limits.

## Sources

- [VseInstrumenti MCP](https://mcp.vseinstrumenti.ru/): address, transport, keyless
  access and what the server does not do. Checked 2026-11-26.
- Connection, tool list and the server's own instructions: Ouroboros's Test on
  2026-11-26.
