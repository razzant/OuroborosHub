"""The owner's path in the browser and what it shows: Dashboard > Consciousness > Connect a place > Telegram."""
from __future__ import annotations

import re

from fake_bot_api import BOT_ID

CONNECT = "dialog.place-connect"
PLACE = "dialog.place-sheet:not(.place-connect)"
READING = "Reading where you can connect"
_ROWS = """(dialog) => [...dialog.querySelectorAll('.place-accounts .place-connector-row')].map((row) => {
    const button = row.querySelector('[data-connect-act]');
    return {label: (row.querySelector('strong')?.textContent || '').trim(), act: button?.dataset.connectAct || '',
            button: (button?.textContent || '').trim(), placeId: button?.dataset.placeId || '', choice: button?.dataset.choice || '',
            disabled: Boolean(button?.disabled), text: row.innerText.replace(/\\s+/g, ' ').trim()};
})"""


def open_connect(page, url: str, *, settle: bool = True) -> None:
    """Dashboard > Consciousness > Connect a place > the Telegram connector, until its list has been read."""
    # The SPA's controls, not all unrelated page resources, establish readiness.
    page.goto(url + "/#dashboard", wait_until="domcontentloaded")
    page.locator('[data-dashboard-tab="consciousness"]').click()
    page.locator("[data-place-connect]:visible").first.click()
    page.locator(f'{CONNECT} [data-connect-act="choose"][data-connector="telegram-bot"]').click()
    page.locator(f'{CONNECT} [data-connect-act="back"]').wait_for()
    if settle:
        settled(page)


def settled(page) -> None:
    """The list read finished (rows, an empty/unavailable note, or nothing offered): not the loading sentence."""
    page.wait_for_function("([sel, reading]) => { const d = document.querySelector(sel); return d && d.open"
                           " && !d.innerText.includes(reading); }", arg=[CONNECT, READING])


def rows(page) -> list:
    return page.locator(CONNECT).evaluate(_ROWS)


def telegram_rows(page) -> list:
    """The rows beyond the connector's own «Whole account» entry."""
    return [row for row in rows(page) if row["choice"] != "whole"]


def sheet_text(page) -> str:
    return page.locator(CONNECT).inner_text()


def names_id(label: str, chat_id: int) -> bool:
    """The label carries this exact signed numeric id as one token: not unsigned, not a longer or shorter number."""
    return re.search(rf"(?<![\w-]){re.escape(str(chat_id))}(?!\d)", label) is not None


def row_with_id(listing: list, chat_id: int) -> dict:
    """The one row naming this exact signed numeric id, else a failure."""
    found = [row for row in listing if names_id(row["label"], chat_id)]
    assert len(found) == 1, f"expected exactly one row for chat {chat_id}, found {len(found)}: {[r['label'] for r in listing]}"
    return found[0]


def press(page, row: dict) -> None:
    selector = (f'{CONNECT} [data-connect-act="open"][data-place-id="{row["placeId"]}"]' if row["act"] == "open"
                else f'{CONNECT} [data-connect-act="connect"][data-choice="{row["choice"]}"]')
    page.locator(selector).click()


def place_sheet(page) -> dict:
    """What the opened Place sheet states for the place: its state button, ticked wake kinds and notes."""
    page.locator(PLACE).wait_for()
    return page.locator(PLACE).evaluate("""(dialog) => ({
        title: (dialog.querySelector('.place-sheet-title')?.textContent || '').trim(),
        state: [...dialog.querySelectorAll('[data-place-state] [data-effort-value]')]
            .filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.dataset.effortValue),
        wakes: [...dialog.querySelectorAll('.place-wake-toggle input:checked')].map((i) => i.dataset.fact),
        notes: [...dialog.querySelectorAll('.place-note-list .place-note')].map((n) => n.innerText.replace(/\\s+/g, ' ').trim()),
        text: dialog.innerText.replace(/\\s+/g, ' ').trim()})""")


def close_sheets(page) -> None:
    for selector in (PLACE, CONNECT):
        if page.locator(selector).count():
            page.locator(f"{selector} [data-place-close]").click()
    page.wait_for_function("() => !document.querySelector('dialog.place-sheet[open]')")


def connect_rows(status: dict) -> list:
    assert "connect" in status, "the status route has no `connect` rows: this skill lacks status.connect"
    assert isinstance(status["connect"], list), status["connect"]
    return status["connect"]


def identity(row: dict) -> tuple:
    return (row["account_id"], row["conversation_id"])


def exact(chat_id: int) -> tuple:
    return (str(BOT_ID), str(chat_id))
