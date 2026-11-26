---
name: lenta-mcp-guide
title: "Lenta MCP: groceries, stores and cart links"
description: "How to add Lenta's official MCP server to Ouroboros: search the Lenta storefront for a delivery address or a store, read product cards and categories, list stores, and create a cart link to order on lenta.com. For shoppers at Lenta in Russia; no account for these, and Lenta names no fee."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants groceries or household goods found and compared at Lenta, a Russian retail chain, a nearby store found, or a cart prepared for ordering on lenta.com, or asks to connect Lenta's MCP server."
model_experience:
  what_model_sees: "A short guide: the server address, how to add it under Settings → Extensions → MCP servers, the tools the server listed on 2026-11-26 and their inputs, and the limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# Lenta MCP

Lenta is a Russian retail chain. Its public MCP server is for shoppers at Lenta: it needs
no account for search and cart links, and Lenta names no fee. An assistant can search the
Lenta storefront for a delivery address or a chosen store, read product cards and the
category tree, list stores, and create a link to a cart that the person opens on
lenta.com to place the order.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from Lenta's server.

## Server

- Address: `https://mcp.lenta.com/mcp`
- Transport: Streamable HTTP
- Sign-in: none for search, stores and cart links; the server listed its tools without
  credentials.
- Price: Lenta's pages name no fee.
- Coverage and language: Lenta stores and delivery zones in Russia; prices in rubles;
  tool texts in Russian.

## Add it to Ouroboros

These steps are for Ouroboros 7.12 or later.

1. Open **Settings → Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `Lenta`, **Server ID** `lenta`, **Transport**
   Streamable HTTP and **Address** `https://mcp.lenta.com/mcp`. Leave the headers empty.
3. Press **Test**. On 2026-11-26 it reported a working connection with 7 tools.
4. Switch on **Use this server** and press **Save**.

## Tools

The server listed seven tools on 2026-11-26 (14:12 UTC; earlier listings that day had
six, without `storefront_user_profile`):

- `storefront_resolve_store`: the nearest Lenta hubs for an address or coordinates, one
  per format. Per the server, hypermarkets have the larger range with delivery usually
  in 2 to 4 hours, supermarkets a smaller one with delivery in 15 to 60 minutes.
- `storefront_products_search`: products by text query, 10 per page, for an address or
  `storeId` (`aliasId` from `storefront_resolve_store`).
- `storefront_product_details`: a product card with composition, nutrition, category,
  barcode and weight data, for an address or `storeId`.
- `storefront_categories`: the storefront's category tree, for an address or `storeId`.
- `storefront_stores_list`: stores with addresses, opening hours and pickup (its `id` is
  the shopping-centre code, not the catalogue `storeId`); the city and text filters are
  optional.
- `storefront_cart_link_create`: a public link to a Lenta cart with 1 to 100 items for
  checkout on lenta.com.
- `storefront_user_profile`: the profile of the owner of a Lenta mcp-token, given to the
  tool as its argument: name, phone, email and date of birth. Per the server, an invalid
  or expired token is an error, and the server does not keep the token. Lenta's pages
  read on 2026-11-26 do not say how to get such a token.

## Limits

- Product search, product cards and categories need a delivery address or `storeId` (the
  `aliasId` returned by `storefront_resolve_store`; `id` from `storefront_stores_list`
  is the shopping-centre code, not a catalogue `storeId`). The server keeps no session
  between calls.
- The cart link does not change anyone's existing cart. It opens a separate screen on
  lenta.com that offers to add the items and continue to checkout, where the person
  orders and pays.
- Pet and alcohol stores are searched only when their `storeId` (`aliasId`) is passed.
- Weighed goods are ordered in the store's quantity steps, not in grams.
- Lenta's pages state no usage limits.

## Sources

- [Lenta MCP](https://mcp.lenta.com/) and Lenta's engineering blog post
  [on Habr](https://habr.com/ru/companies/lentatech/articles/1107198): address, tools and
  cart-link behaviour. Checked 2026-11-26.
- Connection, tool list and tool descriptions: Ouroboros's Test on 2026-11-26.
