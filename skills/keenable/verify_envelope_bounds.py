"""Assert that every result envelope fits the host's tool-result window.

Run it from the skill directory:

    python3 verify_envelope_bounds.py

Exits 0 when every shape fits, 1 with the offending sizes otherwise. No network:
each case drives ``keenable_client`` through an in-process fake MCP transport.

WHY THIS FILE SHIPS INSIDE THE PAYLOAD. The host caps a generic tool result at
``ouroboros/tool_capabilities.py::DEFAULT_TOOL_RESULT_LIMIT`` = 15000 characters and
extension tool results are not exempt. If an envelope crosses that line the HOST
truncates it -- including, potentially, the very disclosure fields that say what was
cut. So the skill's own bounds must be the binding ones, and four constants decide
that: ENVELOPE_CHAR_BUDGET, RESULTS_TEXT_BUDGET, CONTENT_CHAR_LIMIT, RAW_TEXT_LIMIT.

Those numbers were measured once during development. A measurement is not a bound:
the next person who raises one of them has no way to know they broke the guarantee.
Documenting the sizes and not shipping the check would be a promise of a guard that
does not exist -- the same defect this skill's envelope was rewritten to remove, one
level up. Hence an executable check, hashed and reviewed with the rest of the
payload rather than living in a scratch directory.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

#: SSOT: ouroboros/tool_capabilities.py::DEFAULT_TOOL_RESULT_LIMIT. Duplicated here
#: deliberately -- a data-plane skill must not import unfrozen host internals -- so
#: it is named with its source and must be re-checked if the host changes it.
HOST_TOOL_RESULT_CAP = 15000

_spec = importlib.util.spec_from_file_location(
    "keenable_client", str(pathlib.Path(__file__).resolve().with_name("keenable_client.py"))
)
if _spec is None or _spec.loader is None:  # pragma: no cover - defensive
    print("FAIL: keenable_client.py not found beside this script")
    raise SystemExit(1)
kc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kc)

_failures: list = []


def _transport(text: str):
    """Minimal in-process MCP server: initialize, notify, then one text blob."""

    def transport(url, body, headers, timeout):
        payload = json.loads(body.decode())
        method = payload.get("method")
        if method == "initialize":
            return 200, {"mcp-session-id": "verify"}, json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {
                    "protocolVersion": kc.PROTOCOL_VERSION, "capabilities": {},
                    "serverInfo": {"name": "envelope-verifier", "version": "1"},
                }})
        if method == "notifications/initialized":
            return 202, {}, ""
        return 200, {}, json.dumps({
            "jsonrpc": "2.0", "id": 2,
            "result": {"content": [{"type": "text", "text": text}]},
        })

    return transport


def _records(count: int) -> str:
    return "\n\n---\n\n".join(
        "Title: %s\nURL: https://example%02d.com/%s\nPublished: 2026-07-25\n"
        "Acquired: 2026-07-30\nSnippets: %s" % ("T" * 120, i, "p" * 60, "s" * 2000)
        for i in range(count)
    )


def _page(chars: int) -> str:
    return "Title: %s\nURL: https://example.com/x\n\n%s" % ("T" * 200, "c" * chars)


def _fits(name: str, envelope: dict) -> None:
    """Assert BOTH bounds: the host cap that binds, and the budget we advertise.

    The skill-owned budget is checked too, because a stated bound that the payload
    itself exceeds is a false disclosure even while the host cap is comfortably
    clear. The one permitted way to be over budget is to SAY so: an envelope
    carrying ``envelope_over_budget_chars`` has declared that both reduction axes
    were spent, which is honest, and is still held to the host cap.
    """
    size = len(json.dumps(envelope, ensure_ascii=False))
    declared_over = envelope.get("envelope_over_budget_chars")
    over_host = size > HOST_TOOL_RESULT_CAP
    over_budget = size > kc.ENVELOPE_CHAR_BUDGET and declared_over is None
    status = "OVER" if (over_host or over_budget) else "ok  "
    print("  %s %-42s serialized=%6d" % (status, name, size))
    if over_host:
        _failures.append("%s serialized to %d > host cap %d" % (name, size, HOST_TOOL_RESULT_CAP))
    if over_budget:
        _failures.append(
            "%s serialized to %d > budget %d with no envelope_over_budget_chars disclosure"
            % (name, size, kc.ENVELOPE_CHAR_BUDGET)
        )


def _check(name: str, condition: bool, detail: object = "") -> None:
    print("  %s %-42s %s" % ("ok  " if condition else "FAIL", name, detail if not condition else ""))
    if not condition:
        _failures.append("%s (%s)" % (name, detail))


print("host tool-result cap: %d   skill envelope budget: %d"
      % (HOST_TOOL_RESULT_CAP, kc.ENVELOPE_CHAR_BUDGET))
_check("budget_leaves_margin_under_host_cap",
       kc.ENVELOPE_CHAR_BUDGET < HOST_TOOL_RESULT_CAP, kc.ENVELOPE_CHAR_BUDGET)

# 1. Search saturating every record bound.
kc.reset_session_cache()
maxed = kc.search(
    {"query": "q" * 80, "site": "example.com",
     "published_after": "2026-01-01", "acquired_after": "2026-01-01"},
    transport=_transport(_records(20)),
)
_fits("search: records at full budget", maxed)
_check("dropped_records_are_disclosed", maxed["results_omitted"] > 0, maxed["results_omitted"])

# 2. Search on a partial parse, where raw vendor text rides on top of the records.
kc.reset_session_cache()
partial = kc.search({"query": "q" * 80},
                    transport=_transport(_records(20) + "\n\n---\n\nOnly: labels here\n"))
_fits("search: partial parse carrying raw", partial)
_check("partial_parse_keeps_raw", bool(partial.get("raw")), partial.get("parse_status"))
_check("raw_limit_is_the_one_that_applied",
       partial["raw_char_limit"] <= kc.RAW_TEXT_LIMIT, partial["raw_char_limit"])

# 3. Search with raw explicitly requested on a clean parse.
kc.reset_session_cache()
_fits("search: include_raw on a clean parse",
      kc.search({"query": "q" * 80, "include_raw": True}, transport=_transport(_records(20))))

# 4. Fetch returning a full page alongside a maximum-length extraction prompt.
kc.reset_session_cache()
full = kc.fetch({"url": "https://example.com/x", "prompt": "p" * kc.PROMPT_MAX_LENGTH},
                transport=_transport(_page(40000)))
_fits("fetch: full page + max prompt", full)
_check("fetch_clip_is_disclosed", full["content_truncated_by_skill"] is True)
_check("vendor_not_asked_beyond_what_we_keep",
       full["max_chars_effective"] <= kc.CONTENT_CHAR_LIMIT, full["max_chars_effective"])

# 5. Fetch where the CALLER's own echoed input is the oversized part. An unbounded
#    echo would make every other bound decorative, so this case exists on purpose.
kc.reset_session_cache()
long_url = "https://example.com/" + "u" * 5000
echo = kc.fetch({"url": long_url}, transport=_transport(_page(40000)))
_fits("fetch: 5000-char url echoed back", echo)
_check("oversized_url_echo_is_disclosed", echo.get("url_truncated") is True, echo.get("url"))

# 6. Fetch where the VENDOR's reported headers are the oversized part.
kc.reset_session_cache()
vendor_headers = "Title: %s\nURL: https://example.com/%s\n\n%s" % ("T" * 6000, "v" * 6000, "c" * 20000)
served = kc.fetch({"url": "https://example.com/x"}, transport=_transport(vendor_headers))
_fits("fetch: 6000-char vendor headers", served)
_check("oversized_served_echo_is_disclosed",
       served["served"].get("served_url_truncated") is True, served["served"].get("served_url"))

# 7. Search where the caller's query and filters are the oversized part.
kc.reset_session_cache()
big_query = kc.search({"query": "q" * 9000, "site": "s" * 9000},
                      transport=_transport(_records(20)))
_fits("search: 9000-char query and filter", big_query)
_check("oversized_query_echo_is_disclosed",
       big_query.get("query_truncated") is True, big_query.get("query_chars_total"))

# 8. An over-ceiling max_chars request must be clamped, not honoured.
kc.reset_session_cache()
over = kc.fetch({"url": "https://example.com/x", "max_chars": 30000},
                transport=_transport(_page(40000)))
_fits("fetch: max_chars=30000 clamped", over)
_check("over_ceiling_request_clamped",
       over["max_chars_effective"] == kc.CONTENT_CHAR_LIMIT and over["max_chars_clamped"] is True,
       over["max_chars_effective"])

# 9. A FAILURE envelope carrying vendor-controlled text. Losing the disclosure here
#    is the worst case: error_class is the field that stops an unread page being read
#    as an absent fact, so it must survive.
def _tool_error_transport(text: str):
    def transport(url, body, headers, timeout):
        payload = json.loads(body.decode())
        method = payload.get("method")
        if method == "initialize":
            return 200, {"mcp-session-id": "verify"}, json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {
                    "protocolVersion": kc.PROTOCOL_VERSION, "capabilities": {},
                    "serverInfo": {"name": "envelope-verifier", "version": "1"},
                }})
        if method == "notifications/initialized":
            return 202, {}, ""
        return 200, {}, json.dumps({
            "jsonrpc": "2.0", "id": 2,
            "result": {"isError": True, "content": [{"type": "text", "text": text}]},
        })

    return transport


kc.reset_session_cache()
failure = kc.fetch({"url": "https://example.com/x"},
                   transport=_tool_error_transport("E" * 60000))
_fits("fetch: 60000-char vendor error message", failure)
_check("failure_envelope_keeps_its_typing",
       failure["ok"] is False and failure["error_class"] == "not_read", failure.get("error_class"))
_check("oversized_error_message_is_disclosed",
       failure.get("message_truncated") is True, failure.get("message_chars_total"))

# 10. An oversized FILTER value must report its original length, not just be shorter.
kc.reset_session_cache()
filtered = kc.search({"query": "q", "site": "s" * 4000}, transport=_transport(_records(3)))
_fits("search: 4000-char filter value", filtered)
_check("oversized_filter_reports_original_length",
       filtered.get("filters_truncated", {}).get("site") == 4000,
       filtered.get("filters_truncated"))
_check("unclipped_filters_report_nothing",
       kc.search({"query": "q", "site": "arxiv.org"},
                 transport=_transport(_records(3))).get("filters_truncated") == {})

# 11. fit_envelope's TRIM path, driven directly.
#
# This is the case whose absence let a real defect through review twice: every shape
# above happens to fit naturally, so the trim branch was never executed by this
# guard, and the branch was where the budget got broken -- it measured the overflow,
# cut exactly that much, then appended ~75 characters of disclosure and returned
# without remeasuring. A test that only exercises payloads which already fit cannot
# see that. Driven directly rather than through a transport because the vendor would
# have to return an implausibly large body to reach it, and the point is the arithmetic.
oversized_fetch = {
    "ok": True, "tool": "fetch_page_content", "auth": "keyless",
    "url": "https://example.com/x", "extraction_mode": "full_page",
    "content": "c" * (kc.ENVELOPE_CHAR_BUDGET + 4000),
    "content_truncated_by_skill": False, "measured": {"content_chars": 0},
    "vendor_content_complete": None, "untrusted_external_data": True,
    "vendor_limits": kc.VENDOR_LIMITS,
}
trimmed = kc.fit_envelope(dict(oversized_fetch), "content")
_fits("fit_envelope: oversized fetch body", trimmed)
_check("trim_is_disclosed",
       int(trimmed.get("envelope_overflow_trimmed") or 0) > 0
       and trimmed.get("content_truncated_by_skill") is True,
       trimmed.get("envelope_overflow_trimmed"))
_check("trim_reaches_the_budget_not_just_the_host_cap",
       len(json.dumps(trimmed, ensure_ascii=False)) <= kc.ENVELOPE_CHAR_BUDGET,
       len(json.dumps(trimmed, ensure_ascii=False)))
_check("trimmed_amount_matches_the_bytes_actually_removed",
       trimmed["envelope_overflow_trimmed"] == len(oversized_fetch["content"]) - len(trimmed["content"]),
       trimmed.get("envelope_overflow_trimmed"))

# The same path on the search side, where records are the second reduction axis.
oversized_search = {
    "ok": True, "tool": "search_web_pages", "auth": "keyless", "query": "q",
    "filters_requested": {}, "filters_truncated": {}, "filter_observations": {},
    "results": json.loads(json.dumps([
        {"title": "t" * 100, "url": "https://example.com/%d" % i, "published": "",
         "acquired": "", "snippet": "s" * 1000, "extra": {}}
        for i in range(12)
    ])),
    "count": 12, "results_omitted": 0, "parse_status": "records",
    "untrusted_external_data": True, "vendor_limits": kc.VENDOR_LIMITS,
    "raw": "r" * 6000,
}
search_trimmed = kc.fit_envelope(dict(oversized_search), "raw")
_fits("fit_envelope: oversized search body + records", search_trimmed)
_check("search_trim_reaches_the_budget",
       len(json.dumps(search_trimmed, ensure_ascii=False)) <= kc.ENVELOPE_CHAR_BUDGET,
       len(json.dumps(search_trimmed, ensure_ascii=False)))
_check("dropped_records_stay_accounted",
       search_trimmed["count"] + search_trimmed["results_omitted"] == 12,
       (search_trimmed["count"], search_trimmed["results_omitted"]))

# The ARGUMENT CONTRACT. Every bound above governs text we RETAIN; this section
# governs what is allowed IN. The two are one guarantee: a non-scalar argument used
# to be forwarded verbatim and echoed verbatim into `filters_requested`, which has
# no length bound and no clip path, and `fit_envelope` can only give back `raw` and
# `results` -- so the one unbounded field could carry the envelope past both its own
# budget and the host cap. These cases need no transport: a rejection must happen
# BEFORE any network leg, and passing a transport that would answer anyway is how a
# test proves the weaker claim by accident.
_HUGE = {"payload": ["x" * 5000] * 4}


def _rejects(name: str, envelope: dict, *, tool: str) -> None:
    """A rejection must be typed, local, and free of the value it rejected."""
    body = json.dumps(envelope, ensure_ascii=False)
    _check(name, (
        envelope.get("ok") is False
        and envelope.get("error") == "keenable_bad_request"
        and envelope.get("error_class") == "local_rejection"
        and envelope.get("tool") == tool
        and "xxxxx" not in body
        and len(body) <= kc.ENVELOPE_CHAR_BUDGET
    ), (envelope.get("error"), envelope.get("error_class"), len(body)))


_rejects("search: object as snippet_max_length refused",
         kc.search({"query": "q", "snippet_max_length": _HUGE}), tool="search_web_pages")
_rejects("search: object as site refused",
         kc.search({"query": "q", "site": _HUGE}), tool="search_web_pages")
_rejects("search: snippet_max_length above vendor max refused",
         kc.search({"query": "q", "snippet_max_length": kc.VENDOR_SNIPPET_MAX_LENGTH + 1}),
         tool="search_web_pages")
_rejects("search: bool as snippet_max_length refused",
         kc.search({"query": "q", "snippet_max_length": True}), tool="search_web_pages")
_rejects("fetch: object as max_chars refused",
         kc.fetch({"url": "https://example.com", "max_chars": _HUGE}), tool="fetch_page_content")
_rejects("fetch: string as live refused",
         kc.fetch({"url": "https://example.com", "live": "yes"}), tool="fetch_page_content")

_rejects("search: string as include_raw refused",
         kc.search({"query": "q", "include_raw": "false"}), tool="search_web_pages")
_rejects("search: object as include_raw refused",
         kc.search({"query": "q", "include_raw": _HUGE}), tool="search_web_pages")

# `include_raw` is a LOCAL option, so the contract has to hold for what it DOES, not
# only for what it refuses. `bool("false")` is True in Python, so the string form
# used to switch raw output on; a real `False` must leave it off, and a real `True`
# must still turn it on.
kc.reset_session_cache()
raw_off = kc.search({"query": "q", "include_raw": False}, transport=_transport(_records(2)))
_check("include_raw_false_keeps_raw_out", "raw" not in raw_off, sorted(raw_off)[:6])
kc.reset_session_cache()
raw_on = kc.search({"query": "q", "include_raw": True}, transport=_transport(_records(2)))
_check("include_raw_true_still_adds_raw", bool(raw_on.get("raw")), raw_on.get("parse_status"))

# One argument, one range: the client floor must equal what the tool schema
# advertises, or the two call paths disagree about the same field.
_rejects("search: snippet_max_length below advertised floor refused",
         kc.search({"query": "q", "snippet_max_length": kc.VENDOR_SNIPPET_MIN_LENGTH - 1}),
         tool="search_web_pages")

# The contract must not have become a wall: a VALID call still reaches the vendor
# and still echoes exactly what was sent.
kc.reset_session_cache()
valid = kc.search(
    {"query": "q", "site": "example.com", "snippet_max_length": 400, "mode": "pro"},
    transport=_transport(_records(2)),
)
_check("valid_scalars_still_pass_through",
       valid.get("ok") is True
       and valid["filters_requested"].get("snippet_max_length") == 400
       and valid["filters_requested"].get("mode") == "pro",
       valid.get("filters_requested"))

# `search(None)` used to raise AttributeError from the `include_raw` read, not from
# any line that looked like it touched the caller's object.
_check("search_none_is_a_typed_error_not_a_crash",
       kc.search(None).get("error") == "keenable_bad_request",
       kc.search(None).get("error"))
_check("fetch_none_is_a_typed_error_not_a_crash",
       kc.fetch(None).get("error") == "keenable_bad_request",
       kc.fetch(None).get("error"))

print()
if _failures:
    print("FAILED (%d):" % len(_failures))
    for item in _failures:
        print("  - " + item)
    sys.exit(1)
print("ALL ENVELOPE BOUNDS HOLD")
