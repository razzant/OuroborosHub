---
name: tinvest-mcp-guide
title: "T-Invest MCP: portfolios, markets and orders"
description: "Connect T-Bank's official T-Invest MCP server from Settings → Extensions → MCP servers. Read portfolio and market data or use a trading token to place and manage orders. Includes sandbox setup and the Russian Trusted Root CA prerequisite."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "The user wants to connect T-Bank's T-Invest MCP server in Ouroboros, understand the bank's access-token options, or fix a certificate verification error when adding or testing the server. Covers the Extra certificate bundle setting for the Russian Trusted Root CA. Not for trading advice; the token is always the user's own, created in T-Bank."
model_experience:
  what_model_sees: "A text guide: endpoints, token scopes, the certificate prerequisite, and the form steps in Settings → Extensions → MCP servers. The skill adds no tools, servers or credentials."
  token_effect: "The file is read as an instruction only: it makes no network calls, adds no tools and changes no schemas. The server's tools appear only in the client where the user added the server with their own token."
---

# T-Invest MCP: portfolios, markets and orders

T-Bank publishes an MCP server for **T-Invest**, its investment platform.
It can read portfolio and market data and, with a token that permits
trading, place and manage orders. This guide connects it to Ouroboros over HTTPS.

**Sources of truth.** Token types, endpoints and the sandbox come from the
[bank's MCP documentation](https://developer.tbank.ru/invest/mcp/); the
`Authorization` header comes from the bank's
[coding-agents page](https://developer.tbank.ru/invest/mcp/agents-connect/coding-agents);
the official root is on the
[Gosuslugi certificate page](https://www.gosuslugi.ru/crt).

## The server

- Production endpoint: `https://invest-public-api.tbank.ru/mcp`.
  Orders here can affect real funds.
- Sandbox endpoint: `https://sandbox-invest-public-api.tbank.ru/mcp`.
  Orders are simulated and do not change your real portfolio. The bank's
  [token guide](https://developer.tbank.ru/invest/intro/intro/token)
  documents a separate Sandbox token for the Invest API.
- Authentication is a bearer token issued by T-Bank. The bank's examples
  send the header `Authorization: Bearer <token>`.

## Access tokens — the user's choice

The bank's documentation lists three access-token types:

- **Read-only** — analytics and portfolio monitoring; cannot place orders.
- **Full access** — lets the connected agent place and manage orders on
  the brokerage accounts authorized by that token.
- **Transfers** — per the bank, full access including top-ups of brokerage
  accounts and transfers between them; see the
  [bank's documentation](https://developer.tbank.ru/invest/mcp/).

Which type to create is the user's own decision. T-Bank lets you limit
read-only or full access to a single account; a Transfers token covers
all accounts. Follow the [bank's token instructions](https://developer.tbank.ru/invest/intro/intro/token)
in the T-Invest web account. Save the token when it is created: the bank
won't display it again. Enter it only in the server configuration below.

## Certificate prerequisite — Russian Trusted Root CA

The endpoints use the **Russian Trusted Root CA**. If the client's default
trust bundle does not include this root, the connection fails with a
certificate verification error.

1. Download the root certificate from the official pages: the
   [Gosuslugi TLS page](https://www.gosuslugi.ru/tls/) links the
   [certificate page](https://www.gosuslugi.ru/crt). On it, choose the
   official **Russian Trusted Root CA**. Use its PEM file. If the official
   download is a binary DER certificate, convert it to PEM first; changing
   the extension isn't enough. Keep the file at a stable path on the
   computer that runs Ouroboros.
2. Open **Settings → This computer → Advanced → Extra certificate bundle**.
   Enter the full path of the PEM file and press **Enter** to save. If you
   already use this setting for another root, use a PEM bundle that also
   contains the certificates you still need.

The setting adds the root to the trust used by the app's model-provider
clients that support it and by HTTPS MCP connections, including their
sign-in. It does not change your browser's or system's trust or turn off
certificate or hostname checks. Settings **Test** uses the saved path
immediately; a task already running keeps the path it started with.

> **Required Ouroboros build:** Development `7.12.2-alpha.0` at
> [commit `ce6fb2c`](https://github.com/razzant/ouroboros/commit/ce6fb2c67ecb838a61e384696d9ae67f265fc458),
> or a later build containing that change. Earlier builds can have the same
> version label but don't apply this setting to remote MCP.

## Connect the server

1. Open **Settings → Extensions → MCP servers**. Turn on **Use MCP
   servers** if it is off, then press **Add server**. This is the global
   switch; the T-Invest entry has its own switch in step 4.
2. In the **Connection** card give the server a **Name** (for example
   `T-Invest`). You can leave **Server ID** blank; **Save** derives it from
   the name. Leave **Transport** on
   **Streamable HTTP**. In the **Address** field paste
   `https://invest-public-api.tbank.ru/mcp`, or the sandbox address above.
3. Open **More: headers, tools, schemas**. In the **HTTP headers** section
   press **Add header**:
   - **Header name**: `Authorization`
   - **Header value**: `Bearer` followed by a space and the access token.
     The field is masked in the interface; the value is sent exactly as
     written, and no scheme is added for you.
4. Press **Test** to check the connection before saving. If the test fails,
   open **Details**: a certificate verification error points to TLS trust;
   HTTP `401` means the request was not authorized. Once Test succeeds,
   switch **Use this server** on in the form, then press **Save**.

## Live verification

On 27 November 2026, Settings → **Test** on the development commit above
reached both bank endpoints with the official root configured. Both returned
HTTP `401` with no token. Before adding the root, the same sandbox test
failed certificate verification. This confirms the HTTPS connection up to
authentication; it does not test an account, tool access, or orders.
