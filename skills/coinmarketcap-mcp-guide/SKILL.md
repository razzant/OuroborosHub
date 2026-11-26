---
name: coinmarketcap-mcp-guide
title: "CoinMarketCap MCP: crypto market data"
description: "How to add CoinMarketCap's official MCP server to Ouroboros: crypto quotes, market and derivatives metrics, technical analysis, news, narratives, holder data and hosted analysis skills. Sign in or use a free API key; market data only."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person wants current crypto prices, market overviews, technical indicators, news or on-chain holder data from CoinMarketCap, or asks to connect its MCP server. Not for trading: the server cannot place orders or move funds."
model_experience:
  what_model_sees: "A short guide: the server address, the two ways to connect under Extensions → MCP servers, the tools the server listed on 2026-11-26, plans and limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# CoinMarketCap MCP

CoinMarketCap runs an MCP server for its crypto market data. An assistant can look up
coins, read current quotes and profiles, check global market, derivatives and holder
metrics, run technical analysis, read coin news and trending narratives, and use a
registry of hosted analysis skills. CoinMarketCap says the server only reads market
data and cannot trade, place orders or move funds.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from CoinMarketCap's server.

## Server

- Address: `https://mcp.coinmarketcap.com/mcp`
- Transport: Streamable HTTP
- Access, either way:
  - **Sign in** with a CoinMarketCap account. Ouroboros registers itself
    automatically; CoinMarketCap's page says signing in sets up a key for you.
  - An API key in the header `X-CMC-MCP-API-KEY`, copied from the CoinMarketCap API
    dashboard. A free Basic key works.
- Plans: Basic is free with 15,000 API credits a month and 50 requests a minute. Paid
  plans start with Builder at US$29 a month (150,000 credits); Startup, Growth and
  Professional cost more, Enterprise by agreement. Credit use depends on the tool.
- CoinMarketCap also offers a pay-per-call address, `https://mcp.coinmarketcap.com/x402/mcp`,
  at 0.01 USDC per call through x402, which needs a funded crypto wallet and an x402
  client. Ouroboros does not pay x402 requests, so use sign-in or a key.

## Add it to Ouroboros

1. Open **Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `CoinMarketCap`, **Server ID**
   `coinmarketcap`, **Transport** Streamable HTTP and **Address**
   `https://mcp.coinmarketcap.com/mcp`.
3. Then choose one way:
   - **With sign-in:** leave the headers empty and press **Save**. The server's page
     shows **Sign-in needed** and a **Sign in** button. Press it; CoinMarketCap's
     authorization page offers to log in to CoinMarketCap or to paste an API key, then
     **Continue**. When the browser comes back, Ouroboros lists the tools. If it does not
     come back, paste the address it stopped on under **Details** and press **Finish
     sign-in**.
   - **With an API key:** open **More: headers, tools, schemas**, press **Add header**,
     enter **Header name** `X-CMC-MCP-API-KEY` and the key as **Header value**, then press
     **Test** and **Save**.
4. Switch on **Use this server**.

On 2026-11-26 the sign-in flow was checked up to CoinMarketCap's authorization page, and
the server refused a deliberately invalid key; no account or real key was used.

## Tools

The pay-per-call address listed 14 tools on 2026-11-26 without an account; the keyed
address lists its own after sign-in or with a key:

- `search_cryptos`: a coin's CoinMarketCap ID from its name, symbol or slug.
- `get_crypto_quotes_latest` and `get_crypto_info`: current quotes in USD and coin
  profiles.
- `search_crypto_info`: search within one coin's website, whitepaper and forum.
- `get_global_metrics_latest` and `get_global_crypto_derivatives_metrics`: market and
  derivatives snapshots.
- `get_crypto_metrics`: holder and address distribution for one coin.
- `get_crypto_technical_analysis` and `get_crypto_marketcap_technical_analysis`:
  indicators for one coin and for the total market cap.
- `get_crypto_latest_news`, `trending_crypto_narratives` and `get_upcoming_macro_events`:
  news for one coin, trending sectors and news-derived upcoming events.
- `find_skill` and `execute_skill`: a registry of hosted analysis services. Searching it
  is free; per the server, a run takes 30 to 300 seconds and is priced differently from
  the direct tools.

## Limits

- All tools share one per-minute budget; the server answers error 1008 when it is
  exceeded.
- Direct tools report USD values over fixed time windows, per the server.
- Commercial use is allowed on Basic through Professional under CoinMarketCap's licence,
  which covers one product and forbids reselling the data as a standalone service.

## Sources

- [CoinMarketCap MCP](https://coinmarketcap.com/api/mcp/) and its
  [tool reference](https://coinmarketcap.com/api/documentation/ai-agent-hub/mcp):
  address, header, sign-in and tools. Checked 2026-11-26.
- [CoinMarketCap API pricing](https://coinmarketcap.com/api/pricing/) and
  [x402](https://coinmarketcap.com/api/x402/). Checked 2026-11-26.
- Sign-in discovery, client registration, the authorization page, the key refusal and
  the tool list: Ouroboros's Test and Sign in on 2026-11-26.
