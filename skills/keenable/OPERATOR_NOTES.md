# Keenable operator notes

## Installation and credentials

Install/update Keenable from OuroborosHub and let the normal lifecycle perform
review, dependency checks, grants where needed, and enablement. The Hub catalog
is the published source; resetting local data removes the installed copy, not
the published payload.

The default public endpoint needs no API key or additional Python dependency.
An optional `KEENABLE_API_KEY` in Settings → Secrets travels as `X-API-Key`.
If that custom secret exists, normal owner-grant rules apply. A configured key
rejected with 401 produces `keenable_auth_invalid_key`; the client does not
silently retry without it. Correcting credentials is an owner action.

Current vendor limits are documented at
[Rate limits](https://docs.keenable.ai/rate-limits): public calls share a per-IP
hourly/per-second pool, authenticated calls use their organization's limits.
The skill does not estimate remaining quota or automatically repeat a 429.
Queries, URLs and extraction instructions leave the machine for Keenable.

## Transport and session state

The skill implements the narrow Streamable HTTP MCP flow it uses, with standard
Python HTTP. It does not import the host's MCP client, require the MCP SDK,
change `MCP_ENABLED`, or need a global server registration. This preserves the
owner's per-skill enablement, normalized results and Hub distribution choice.

1. Send `initialize` and validate the JSON-RPC response and supported protocol.
2. Cache the negotiated version with the optional session ID and key fingerprint.
3. Send `notifications/initialized`, then `tools/call`. Subsequent requests carry
   `MCP-Protocol-Version`; `Mcp-Session-Id` is sent only when the server issued it.

The 2026-10-04 vendor response is a successful InitializeResult without a
session header. That is permitted by MCP. Version 0.3.1 incorrectly required
that header, so installed search/fetch calls failed before tools/call.
The regression fixture preserves that real initialization shape without network
identifiers. Stateful peers remain covered by synthetic transport tests.

A successful stateless connection is initialized, even though its ID is absent.
The negotiated version is the cache-ready fact; no second ready flag is needed.
Each call uses its own ID/version snapshot. Unload and key changes clear or
replace the cached negotiation. Network I/O remains outside the cache lock;
concurrent first calls may each initialize, as before.

Only loss of an issued session triggers the existing one-time reinitialization
and retry. A stateless 404 is not evidence of an expired session. Arbitrary
400/429/5xx responses and unknown transport outcomes are not replayed. If a
provider later switches from stateless operation to requiring sessions with an
untyped 400, unload/re-enable can clear negotiation; this repair does not guess
that every invalid request is a lost session.

One 80-second operation budget covers initialization, notification, call and
any permitted recovery. HTTP legs are capped at 45 seconds and remaining budget;
the notification has a 10-second cap. Tools register 90 seconds for dispatch.
These are separate from the host's module-source loading timeout; a widget fetch
must not introduce a shorter bound that abandons the still-running operation.

References: [MCP transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
and [lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle).

## Result ownership

`keenable_client.py` owns transport, argument normalization, error taxonomy,
parsing, measurements and disclosed bounds. Both tools return its JSON envelope.
The widget routes use the same client and add only presentation guidance/state.
The module renders vendor strings as text, not executable markup.

`SKILL.md` documents the result contract and when to choose each tool. Important
invariants are unchanged: missing content is not a negative research finding;
`vendor_content_complete` stays null; skill clipping and provider completeness
are different; requested filters and observed metadata are different. Redirects,
short bodies and low link counts are observations that need interpretation.

The text parser remains intentionally vendor-specific. The current endpoint's
normal and empty text replies were exercised. Full streaming-event correlation,
structuredContent-only replies and a future JSON output format are not newly
implemented or claimed. The skill does not send the vendor's unrelated
`session_id` grouping argument.

## Widget architecture

The owner requested a functional, modern interface on 2026-10-04. The module
replaces the declarative two-form layout because a result's exact URL could not
be bound to a Read action in that layout. The skill/tab identity is unchanged.
`plugin.py` owns the literal declaration; `widget.js` is a classic-script entry.
It uses the host's existing opaque frame, route bridge, opener, theme signal and
dispose hook. No core changes, new daemon, polling or browser-storage workaround
are needed.

The optional author-kit route reads the installed shared control stylesheet at
request time. Ordinary controls consume its classes and tokens; layout belongs
to the widget. The first view remains usable while shared styling or saved state
loads, and restore never replaces a draft the owner has already edited. Search,
Read and Ask use button handlers and composition-aware Enter handling rather
than relying on native form submission inside the sandbox.

Draft/view snapshots and server-produced results have separate storage owners
under `api.get_state_dir()`. Atomic writes preserve complete JSON, and client
draft saves cannot overwrite a just-completed server result. Page text and its
optional generated answer remain separate. The latest view is shared per
installation, with last-successful-write behavior across windows; no multi-user
history or coordination store is introduced. The module flushes pending drafts
on dispose and unsubscribes theme/listeners. Disabling the skill can reject a
late save, and terminating the process can interrupt work; neither is reported
as a successful persistence.

The core author-kit and module lifecycle examples are the compatibility seam,
not copied host shell code. Current-host light/dark and narrow/wide rendering
need visual acceptance. A browser rendering does not alone certify every native
shell; older missing optional bridge features use the documented basic fallback.

## Verification and delivery

From the Hub root, use the repository's ordinary unittest discovery and catalog
validator. `tests/test_keenable_transport.py` covers real stateless initialization,
stateful sessions, cache/reset, malformed responses, errors and the shared budget.
It also invokes the existing `verify_envelope_bounds.py`, so its 45 checks are no
longer just a manual script. Test fakes return valid InitializeResults; production
validation is never weakened to accommodate an incomplete fake.

Widget tests cover route/state ownership and the module contract. Visual checks
must run the actual host frame/bridge with the candidate, in light/dark and narrow
cards, including Search → Read → Ask → Back, keyboard use, failures and remount.
Test fixtures and evidence belong outside the installed skill payload.

A live keyless search/fetch checks the final client against the provider. A
catalog check only verifies package metadata; it is not an installed skill review.
After the PR merges, update through the normal Hub lifecycle, confirm the
installed version/hash, review authority, enablement and loaded extension, then
exercise both tools and the visible widget. Never replace those facts with a
successful download or a passing standalone unit suite.
