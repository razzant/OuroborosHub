---
name: tutu-mcp-guide
title: "Tutu MCP: trains, flights, buses and hotels"
description: "How to add Tutu.ru's official MCP server to Ouroboros: search flights, Russian Railways trains, buses, commuter trains and hotels, compare routes, read seat maps and get a checkout link to tutu.ru. No account; an opt-in tool registers passengers and creates an unpaid order."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants tickets or hotels found and compared on Tutu.ru, a Russian travel service, or asks to connect Tutu's MCP server. Payment always happens on tutu.ru through a link."
model_experience:
  what_model_sees: "A short guide: the server address, how to add it under Extensions → MCP servers, the tools the server listed on 2026-11-26, which one has side effects, and its limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# Tutu MCP

Tutu.ru is a Russian travel booking service. Its public MCP server lets an assistant
search flights, Russian Railways trains, intercity buses, commuter trains and hotels,
compare transport modes for one route, read offer details and train seat maps, and
give the person a checkout link to tutu.ru. The person pays on tutu.ru; the server
takes no payments.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from Tutu's server.

## Server

- Address: `https://mcp.tutu.ru/mcp`
- Transport: Streamable HTTP
- Sign-in: none. Tutu's page says the server works without authorization.
- Price: Tutu's page names no fee.
- Coverage and language: Tutu.ru's own offers. The vendor page is in Russian; the tool
  descriptions are in English.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `Tutu`, **Server ID** `tutu`, **Transport**
   Streamable HTTP and **Address** `https://mcp.tutu.ru/mcp`. Leave the headers empty.
3. Press **Test**. On 2026-11-26 it reported a working connection with 17 tools.
4. Press **Save** and switch on **Use this server**.

## Tools

The server listed 17 tools on 2026-11-26.

Search and details, read-only:

- `search_avia`, `search_rail`, `search_bus`, `search_etrain`, `search_hotels`: one
  kind of offer each.
- `search_multitransport`: flights, trains, buses and commuter trains for one route in
  a single call.
- `get_offer_details` and `get_rail_seatmap`: the details of one offer and the car and
  seat layout of a train.
- `get_avia_instructions`, `get_rail_instructions`, `get_bus_instructions`,
  `get_etrain_instructions`, `get_hotels_instructions` and
  `get_multitransport_instructions`: the server's own playbooks, which it asks the
  assistant to read before the first search of each kind.
- `fetch_resource`: reads the server's `tutu://` resources for clients that do not show
  them.

Checkout:

- `create_checkout_link` is the default hand-off. It returns a link to tutu.ru for the
  chosen offer; the cart is created in the person's own browser.
- `register_checkout_passengers` is the one tool with side effects. With passport data
  that the person provides, it registers the passengers, fills the cart and returns a
  payment link. For a flight it places an unpaid booking; for a train it reserves the
  chosen seats, and each repeated call holds more seats. It never charges money. The
  server's instructions ask the assistant to read the passenger data and the trip back
  and to get an explicit yes before calling it.

## Limits

- Payment is made only by the person, on tutu.ru.
- Passport data reaches Tutu only through `register_checkout_passengers`.
- Tutu's page states no usage limits.

## Sources

- [Tutu MCP](https://mcp.tutu.ru/mcp): the same address serves Tutu's connection page
  in a browser, with the transport and keyless access. Checked 2026-11-26.
- Connection, tool list and the server's own instructions: Ouroboros's Test on
  2026-11-26.
