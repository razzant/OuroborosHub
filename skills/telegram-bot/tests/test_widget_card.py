"""The Widgets card: a parseable widget.js, bridge-only requests and real status fields."""
import asyncio
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

from test_plugin import FakeApi

SKILL = pathlib.Path(__file__).resolve().parents[1]
WIDGET = SKILL / "widget.js"
KIT = re.compile(r"/\* transport-kit:begin.*?/\* transport-kit:end \*/", re.S)
NODE = shutil.which("node") or os.environ.get("OUROBOROS_PLAYWRIGHT_NODE_SELECTED") or ""


def _registered(tmp_path):
    spec = importlib.util.spec_from_file_location("telegram_bot_widget_card_test", SKILL / "plugin.py",
                                                  submodule_search_locations=[str(SKILL)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    return module, api


@pytest.mark.skipif(not NODE or not pathlib.Path(NODE).exists(), reason="node is not installed")
def test_widget_parses():
    subprocess.run([NODE, "--check", str(WIDGET)], check=True)


def test_widget_follows_the_module_contract():
    source = WIDGET.read_text(encoding="utf-8")
    assert set(re.findall(r"['\"](/api/[^'\"]*)['\"]", source)) == {"/api/extensions/telegram-bot/"}
    assert "BASE + 'status'" in source and "BASE + 'settings/save'" in source
    assert "bridge.fetch" in source and "XMLHttpRequest" not in source and "WebSocket" not in source
    assert "typeof bridge.onTheme === 'function'" in source
    assert re.search(r"data-theme\s*=\s*[\"']?light", source)
    assert "html,body{margin:0;background:transparent}" in source and "__ouroWidgetOnDispose" in source
    assert "max_ticks" not in source and "refresh · showing" in source and "Try again" in source
    assert "ouro-widget-resize" not in source and "ResizeObserver" not in source
    assert "word-break:normal" in source and source.count("overflow-wrap:anywhere") == 1


def test_every_truthful_fact_of_the_old_card_is_kept():
    source = re.sub(r"'\s*\+\s*'", "", WIDGET.read_text(encoding="utf-8"))  # join split string literals
    for words in ("Telegram labels this account as a bot", "group coverage depends on privacy mode and admin",
                  "A bot cannot start a private chat with someone who never opened one",
                  "The Bot API cannot read earlier chat history", "separate Telegram participant connector",
                  "Management group ID", "Marks this room in event facts. Work instructions stay in the place notes; "
                  "no owner permissions are granted.", "Telegram offset", "Reactions in Host context"):
        assert words in source, words
    for field in ("inbox_waiting", "inbox_leased", "inbox_submitted", "inbox_failed", "outbox_waiting", "outbox_held",
                  "outbox_delivered", "outbox_failed", "outbox_uncertain", "telegram_offset", "bot_label",
                  "last_event_at", "last_delivery_at", "last_error", "reaction_context"):
        assert f"data.{field}" in source, field


def test_the_group_form_validates_before_posting_and_reports_the_result():
    source = WIDGET.read_text(encoding="utf-8")
    assert "/^-[0-9]+$/" in source and "method: 'POST'" in source
    assert "JSON.stringify({ management_group_id: value })" in source
    assert "'Saved.'" in source and "'Not saved: '" in source


def test_shared_kit_is_marked_once_and_matches_slack_when_both_are_present():
    ours = KIT.findall(WIDGET.read_text(encoding="utf-8"))
    assert len(ours) == 1
    # The two cards ship in separate PRs: compare only once both carry the kit (skills also install separately).
    sibling = SKILL.parent / "slack-bridge" / "widget.js"
    theirs = KIT.search(sibling.read_text(encoding="utf-8")) if sibling.exists() else None
    if theirs is None:
        pytest.skip("slack-bridge's widget.js with the transport kit is not beside this skill")
    assert theirs.group(0) == ours[0]


def test_every_status_field_the_card_reads_exists_on_the_route(tmp_path):
    module, api = _registered(tmp_path)
    try:
        payload = json.loads(asyncio.run(module._status_route(None)).body)
    finally:
        api.unload()
    read = set(re.findall(r"\bdata\.([a-z_]+)", WIDGET.read_text(encoding="utf-8")))
    optional = {"identity", "connect_unavailable"}  # present only in some states (plugin.py)
    assert sorted(field for field in read - optional if field not in payload) == []


def _source():
    return re.sub(r"'\s*\+\s*'", "", WIDGET.read_text(encoding="utf-8"))  # join split string literals


def test_requests_go_only_through_the_bridge():
    source = WIDGET.read_text(encoding="utf-8")
    # No fallback to the frame's own fetch: without the bridge the card says so (and Save says why it failed).
    assert "window.fetch" not in source and "fetch(" not in source.replace("bridge.fetch", "")
    assert "The widget bridge is unavailable" in source


def test_send_advice_never_suggests_a_blind_full_resend():
    source = _source()
    # Part of a long message or an album may have arrived: the receipt's confirmed parts decide what is missing.
    assert "confirmed_parts" in source and source.count("only what is missing") == 2
    assert "send again if needed" not in source and "decide to send ' + them + ' again" not in source
    for kind in ("outcome_unknown", "not_delivered"):
        branch = source.split(f"item.kind === '{kind}'")[1].split("} else if")[0]
        assert "PART_SENT" in branch, kind


def test_link_advice_matches_the_side_the_poll_failed_on():
    source = _source()
    lines = source.split("var POLL_LINES = {")[1].split("};")[0]
    sides = {side: lines.split(f"{side}: [")[1].split("]")[0] for side in ("token", "network", "telegram", "local")}
    assert "TELEGRAM_PUBLIC_BOT_TOKEN" in sides["token"] and "network" not in sides["token"]
    assert "network" in sides["network"] and "token" not in sides["network"].lower()
    assert "Not a token or network problem" in sides["local"] and "Telegram answered" in sides["local"]
    # Without a known side the generic words name neither the token nor the network.
    generic = source.split("provider_error: [")[1].split("]")[0]
    assert "token" not in generic and "network" not in generic
    assert "POLL_LINES[item.failure]" in source


def test_unknown_facts_never_read_as_none_or_unsupported():
    source = _source()
    assert "row('Early answers', !checked ? 'not checked yet'" in source
    assert "(countsKnown ? 'none yet' : '—')" in source and source.count("(countsKnown ? 'none yet' : '—')") == 2


def test_empty_wording_says_what_is_actually_known():
    source = _source()
    # Membership updates move the offset without any message; a removed bot offers no chats.
    assert "Nothing received or sent yet" not in source and "none seen yet" not in source
    assert "No messages received or sent yet." in source and "'no chats available'" in source


def test_the_owners_hold_reason_is_labelled_and_kept_first():
    source = _source()
    assert "'Needs you:'" in source
    assert "filter(function (reason) { return reason.action; })" in source
