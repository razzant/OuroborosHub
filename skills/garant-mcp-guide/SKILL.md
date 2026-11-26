---
name: garant-mcp-guide
title: "GARANT MCP: Russian legal database"
description: "How to add GARANT's official MCP server to Ouroboros with an API token: search Russian legislation and court practice in the GARANT system, read and export documents, track changes, add links to legal references in a text and read the PRIME news feed. For GARANT subscribers with Garant Connect (API) access: no separate MCP price; Garant Connect is sold inside regional LegalTech service packages, and the servicing organization issues the token."
version: 0.1.0
type: instruction
permissions: []
when_to_use: "A person with GARANT API access wants Ouroboros to search or cite Russian legal documents and court practice in the GARANT system, or asks how to connect GARANT's MCP server. Needs a GARANT API token."
model_experience:
  what_model_sees: "A short guide: the server address, how to get a token and add it as a header under Settings → Extensions → MCP servers, the tools the server listed on 2026-11-26 and GARANT's monthly limits. The guide adds no tools, servers or credentials."
  token_effect: "Read only as an instruction. Tools come from the server the person adds; Ouroboros loads their call schemas when needed unless Schemas from task start is on."
---

# GARANT MCP

GARANT is a Russian legal reference system. Its MCP server is a second way into the
Garant Connect API and is for GARANT subscribers with API access: there is no separate
MCP price, Garant Connect is sold inside LegalTech service packages as part of the
GARANT system (with regional pricing through GARANT partners), and GARANT's servicing
organization issues the API token. An assistant can search legislation and court
practice, find the relevant parts of a document, export documents, check what changed,
add links to the GARANT documents a text cites, and read the PRIME news feed. It uses
the same access rights and limits as the API.

This entry is a guide and adds no tools by itself. Add the server once as described
below; the tools then come from GARANT's server.

## Server

- Address: `https://mcp.garant.ru/`. GARANT's documentation names the host
  `mcp.garant.ru`; on 2026-11-26 the address `https://mcp.garant.ru/mcp` answered the
  same way.
- Transport: Streamable HTTP
- Token: the GARANT API token, which GARANT's servicing organization sends by email on
  request. It goes in the header `Authorization` as `Bearer <token>`.
- Price: no separate MCP price. On GARANT's LegalTech site (`lt.garant.ru`, published
  by OOO "GARANT-SERVIS-UNIVERSITET"), the Garant Connect service is sold inside
  LegalTech service packages as part of the GARANT system: 2,000 ₽ a month for 2
  services, 4,600 ₽ for 5 services, or 6,800 ₽ for 10 services in Moscow and the Moscow
  region (with a 3-day free trial); prices in other regions come from GARANT's regional
  partners.
- Language: Russian documents.

## Add it to Ouroboros

These steps are for Ouroboros 7.12 or later.

1. Open **Settings → Extensions → MCP servers** and turn on **Use MCP servers** if it is off.
2. Press **Add server** and fill in **Name** `GARANT`, **Server ID** `garant`,
   **Transport** Streamable HTTP and **Address** `https://mcp.garant.ru/`.
3. Open **More: headers, tools, schemas**, press **Add header** and enter **Header
   name** `Authorization` with **Header value** `Bearer <token>`.
4. Press **Test**, then switch on **Use this server** and press **Save**.
5. Because the server lists its tools even with a placeholder token, a passing Test does
   not check the token; confirm access by asking the assistant for a simple read such as
   checking remaining limits (`garant_limits`).

On 2026-11-26 the server answered HTTP 401 without a token and listed 18 tools to Test
with a placeholder token; no real token was used, so no call was tried.

## Tools

The server listed 18 tools on 2026-11-26:

- `garant_search`: full-text search in the main GARANT collection or the court practice
  bank.
- `garant_snippets`: returns the matching block IDs and table-of-contents headings in a
  document for a phrase or citation, not the block text itself; reading the block text
  uses `garant_export_block_html`, which counts toward the export quota.
- `garant_document_info` and `garant_document_redactions`: a document's metadata and its
  editions.
- `garant_export_html`, `garant_export_block_html`, `garant_export_pdf`,
  `garant_export_rtf` and `garant_export_odt`: a whole document or one part of it;
  `garant_export_image` and `garant_export_formula` for its pictures and formulas.
- `garant_find_hyperlinks`: adds links to the GARANT documents a text refers to.
- `garant_find_modified` and `garant_block_changed`: which documents or parts changed
  after a date.
- `garant_prime_categories` and `garant_prime_news`: the PRIME news feed.
- `garant_sutyazhnik`: court decisions relevant to a text, through GARANT's Sutyazhnik
  service.
- `garant_limits`: the token's remaining requests.

## Limits

GARANT's limits for the Internet version apply through the server too:

- Adding links and checking documents or parts on control: 1,000 requests a calendar
  month in total. `garant_find_modified` takes at most 100 documents per call (sending
  more than 100 returns HTTP 400).
- Exports (including `garant_export_block_html`): 30 documents or parts a calendar month.
- Document information: 300 requests a calendar month.
- PRIME news: not older than one year.
- Text for adding links and request bodies: up to 20 MB.

## Sources

- [GARANT API documentation](https://www.garant.ru/mobileonline/api/documentation),
  sections on API access, limits and «MCP API (mcp.garant.ru)»: host, token, tools and
  limits. Checked 2026-11-26.
- [Garant Connect](https://lt.garant.ru/prod/connect) and
  [GARANT LegalTech packages](https://lt.garant.ru/) (published by OOO
  "GARANT-SERVIS-UNIVERSITET"): Garant Connect service overview, MCP integration and
  regional package pricing. Checked 2026-11-26.
- Server answers and the tool list: Ouroboros's Test on 2026-11-26.
