"""One scenario on a host that starts with no groups: unknown account, then known-empty, then an unreadable list.

``connect: null`` is *unknown* (the provider has not confirmed the bot: ``getMe`` fails), ``connect: []`` is a
known empty list, and a failed status read is a third thing. The owner's sheet says each in its own words. One
ordered scenario on one host, because each step is the next one's starting state.
"""
from __future__ import annotations

import json

import ui

UNAVAILABLE = "Could not read the connector’s list."
EMPTY = "None offered yet."


def test_unknown_account_then_known_empty_then_unreadable_list(unconfirmed_host, page, record):
    host = unconfirmed_host
    # 1. the provider cannot say who the bot is: unknown, never an empty list
    unknown = host.status()
    assert "connect" in unknown and not isinstance(unknown["connect"], list), unknown.get("connect", "<absent>")
    ui.open_connect(page, host.url)
    text = ui.sheet_text(page)
    assert UNAVAILABLE in text and EMPTY not in text
    assert ui.telegram_rows(page) == []
    shots = {"unknown-account": page.shots("unknown-account")}

    # 2. the provider confirms the bot: a known list with nothing in it
    host.bot.get_me_fails = False
    host.bot.wait_call("getUpdates")  # polling as the confirmed account (none was possible before)
    known = host.status()
    assert known["connect"] == []
    page.locator(f'{ui.CONNECT} [data-connect-act="retry-choices"]').click()
    ui.settled(page)
    text = ui.sheet_text(page)
    assert EMPTY in text and UNAVAILABLE not in text and ui.telegram_rows(page) == []
    shots["empty"] = page.shots("empty")

    # 3. the status read itself fails (a browser-side fault on the declared route): unavailable, then Try again reads
    pattern = "**/api/extensions/telegram-bot/status*"
    page.route(pattern, lambda route: route.fulfill(
        status=500, content_type="application/json", body=json.dumps({"ok": False, "error": "synthetic read failure"})))
    ui.open_connect(page, host.url)
    assert UNAVAILABLE in ui.sheet_text(page) and ui.telegram_rows(page) == []
    assert page.locator(f'{ui.CONNECT} [data-connect-act="retry-choices"]').count() == 1
    shots["unreadable"] = page.shots("unreadable")
    page.unroute(pattern)
    page.locator(f'{ui.CONNECT} [data-connect-act="retry-choices"]').click()
    ui.settled(page)
    assert UNAVAILABLE not in ui.sheet_text(page) and EMPTY in ui.sheet_text(page)
    assert host.bot.sends == []
    record(f"empty-and-unknown-{host.engine}", {"unknown": unknown, "known": known, "shots": shots})
