---
name: trivago-mcp-guide
title: "trivago MCP: hotel search and price trends"
description: "How to add trivago's official MCP server to Ouroboros: hotel search across booking sites with prices and booking links, and monthly price forecasts for a destination. For anyone planning a stay; free, no account, read-only."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants to find or compare hotels, check prices for dates or plan when to travel, and asks to use trivago or to connect its MCP server. Not for booking or payment: trivago returns links to the booking sites."
model_experience:
  what_model_sees: "A short guide: the server address, how to add it under Extensions → MCP servers, the tools the server listed on 2026-11-26 and its limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# trivago MCP

trivago runs a public MCP server for its hotel metasearch. It is for anyone planning a
stay, and it is free and needs no account. An assistant can search hotels and other
stays around a place or by name, compare the prices trivago collects from booking sites,
and forecast nightly prices for a destination. Results link to the booking sites, where
the person books and pays.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from trivago's server.

## Server

- Address: `https://mcp.trivago.com/mcp`
- Transport: Streamable HTTP
- Sign-in: none, and no API key
- Price: free, according to trivago
- Coverage: trivago's hotel listings. The server asks the assistant to pass the user's
  own country, currency and language, chosen from the values it lists.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `trivago`, **Server ID** `trivago`,
   **Transport** Streamable HTTP and **Address** `https://mcp.trivago.com/mcp`. Leave
   the headers empty.
3. Press **Test**. On 2026-11-26 it reported a working connection with 3 tools.
4. Switch on **Use this server** and press **Save**.

## Tools

The server listed three read-only tools on 2026-11-26:

- `trivago-accommodation-radius-search`: stays around coordinates such as a city centre,
  a district, a landmark or an address. The server's own instructions prefer it for
  hotel searches.
- `trivago-accommodation-search`: stays by property name or free text, or for a region
  too large for one point.
- `trivago-destination-price-trends`: monthly average, minimum and maximum nightly
  rates for a destination by star rating. It is a forecast, not live availability.

## Limits

- Search and links only: the server books nothing, takes no payment and signs in to
  nothing.
- trivago's page states no usage limits.

## Sources

- [trivago MCP](https://mcp.trivago.com/docs): address, free keyless use, tool
  descriptions and supported clients. Checked 2026-11-26.
- Connection, tool list and the server's own instructions: Ouroboros's Test on
  2026-11-26.
