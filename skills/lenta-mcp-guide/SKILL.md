---
name: lenta-mcp-guide
title: "Lenta MCP: groceries, stores and cart links"
description: "How to add Lenta's official MCP server to Ouroboros: search the Lenta storefront for a delivery address or a store, read product cards and categories, list stores, and create a cart link to order on lenta.com. No account."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants groceries or household goods found and compared at Lenta, a Russian retail chain, a nearby store found, or a cart prepared for ordering on lenta.com, or asks to connect Lenta's MCP server."
model_experience:
  what_model_sees: "A short guide: the server address, how to add it under Extensions → MCP servers, the tools the server listed on 2026-11-26 and its limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# Lenta MCP

Lenta is a Russian retail chain. Its public MCP server lets an assistant search the
Lenta storefront for a delivery address or a chosen store, read product cards and the
category tree, list stores, and create a link to a cart that the person opens on
lenta.com to place the order.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from Lenta's server.

## Server

- Address: `https://mcp.lenta.com/mcp`
- Transport: Streamable HTTP
- Sign-in: none; the server listed its tools without credentials.
- Price: Lenta's pages name no fee.
- Coverage and language: Lenta stores and delivery zones in Russia; prices in rubles;
  tool texts in Russian.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `Lenta`, **Server ID** `lenta`, **Transport**
   Streamable HTTP and **Address** `https://mcp.lenta.com/mcp`. Leave the headers empty.
3. Press **Test**. On 2026-11-26 it reported a working connection with 6 tools.
4. Press **Save** and switch on **Use this server**.

## Tools

The server listed six tools on 2026-11-26:

- `storefront_resolve_store`: the nearest Lenta hubs for an address or coordinates, one
  per format. Per the server, hypermarkets have the larger range with delivery usually
  in 2 to 4 hours, supermarkets a smaller one with delivery in 15 to 60 minutes.
- `storefront_products_search`: products by text query, 10 per page.
- `storefront_product_details`: a product card with composition, nutrition, category,
  barcode and weight data.
- `storefront_categories`: the storefront's category tree.
- `storefront_stores_list`: stores with addresses, opening hours and pickup.
- `storefront_cart_link_create`: a public link to a Lenta cart with 1 to 100 items for
  checkout on lenta.com.

## Limits

- Every call needs an address or a store ID: the server keeps no session.
- The cart link does not change anyone's existing cart. It opens a separate screen on
  lenta.com that offers to add the items and continue to checkout, where the person
  orders and pays.
- Pet and alcohol stores are searched only when their store ID is passed.
- Weighed goods are ordered in the store's quantity steps, not in grams.
- Lenta's pages state no usage limits.

## Sources

- [Lenta MCP](https://mcp.lenta.com/) and Lenta's engineering blog post
  [on Habr](https://habr.com/ru/companies/lentatech/articles/1107198): address, tools and
  cart-link behaviour. Checked 2026-11-26.
- Connection, tool list and tool descriptions: Ouroboros's Test on 2026-11-26.
