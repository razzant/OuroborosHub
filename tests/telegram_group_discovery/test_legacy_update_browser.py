"""A group the published skill 0.8 captured and left pending is offered after the owner updates the skill.

The host starts with the skill as the Hub published it (``git archive`` of ``TG_DISCOVERY_LEGACY_REF``, which
has no ``status.connect``); a raw update from a group nobody connected is captured by that runtime. The payload
under test then replaces it the way an update does (off, new bytes with synthetic admission records, on) and the same
owner path must offer that group by exact identity. Only public surfaces are read: the status route and the page.
"""
from __future__ import annotations

import json

import conftest
import ui
import updates as U
from fake_bot_api import BOT_ID

PENDING = (-1002000000777, "Pending since the old skill")
# A backlog far past any one read: many groups, and one group that said a great deal.
FLOOD = (-1002000000778, "The chatty group")
SCATTERED = [(-1005000000000 - n, f"Old group {n:02d}") for n in range(30)]


def test_group_pending_under_the_old_skill_is_offered_after_update(legacy_host, page, record):
    host = legacy_host
    last = max(host.bot.push(update) for update in [
        U.group_message(*PENDING, "heard while the old skill ran"),
        *(U.group_message(chat, title, "from the old days") for chat, title in SCATTERED),
        *(U.group_message(*FLOOD, f"message {n}") for n in range(400))])
    host.bot.wait_acked(last)
    old = host.status()
    assert "connect" not in old, "the starting skill already has status.connect: it is not the published 0.8"
    assert old["inbox_waiting"] >= 1 and old["last_event_at"], "the old runtime did not capture the pending group's update"
    sends = len(host.bot.sends)

    installed = host.update_skill(conftest.SKILL)
    after = host.status()
    offered = ui.connect_rows(after)
    record(f"legacy-update-{host.engine}", {"before": old, "installed": installed, "after": after})
    expected = {ui.exact(chat) for chat, _ in [PENDING, FLOOD, *SCATTERED]}
    assert {ui.identity(r) for r in offered} == expected and len(offered) == len(expected), offered[:3]

    ui.open_connect(page, host.url)
    row = ui.row_with_id(ui.telegram_rows(page), PENDING[0])
    assert row["act"] == "connect" and PENDING[1] in row["label"]
    shots = page.shots("legacy-pending")
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/places")) as sent:
        ui.press(page, row)
    assert json.loads(sent.value.post_data)["conversation_id"] == str(PENDING[0])
    assert ui.place_sheet(page)["state"] == ["silent"]
    places = host.places()
    assert [(p["account_id"], p["conversation_id"], p["state"]) for p in places] == [(str(BOT_ID), str(PENDING[0]), "silent")]
    assert len(host.bot.sends) == sends
    record(f"legacy-connected-{host.engine}", {"shots": shots})
