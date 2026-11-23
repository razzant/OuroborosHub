"""The Widgets card: one module declaration, a parseable widget.js, bridge-only requests and real status fields."""
from __future__ import annotations

import os
from pathlib import Path
import re

import pytest

from lib.store import BridgeStore
from test_status_contract import _read_status_route, _TokensApi
from test_workers_plugin import _Api, _load_plugin

SKILL = Path(__file__).resolve().parents[1]
WIDGET = SKILL / "widget.js"
RENDER = {"kind": "module", "entry": "widget.js", "appearance": "host", "start": "auto", "span": 1}


def _frontmatter() -> str:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    return text.split("---", 2)[1]


def test_skill_manifest_and_runtime_register_the_same_single_module_card(tmp_path):
    front = _frontmatter()
    block = front[front.index("\nui_tab:"):]
    for line in ("tab_id: slack_presence", "title: Slack Bridge", "span: 1", "kind: module", "entry: widget.js",
                 "appearance: host", "start: auto"):
        assert line in block
    api = _Api(tmp_path)
    _load_plugin().register(api)
    assert list(api.tabs) == ["slack_presence"]
    assert api.tabs["slack_presence"][0] == "Slack Bridge"
    assert api.tabs["slack_presence"][1]["render"] == RENDER


def test_widget_follows_the_module_contract():
    source = WIDGET.read_text(encoding="utf-8")
    # Requests go only through the bridge to this skill's own routes.
    urls = set(re.findall(r"['\"](/api/[^'\"]*)['\"]", source))
    assert urls == {"/api/extensions/slack-bridge/status"}
    assert "OuroborosWidget" in source and "bridge.fetch" in source
    assert "XMLHttpRequest" not in source and "WebSocket" not in source
    # Host appearance: theme through the bridge and a transparent page (the light palette and disposal are
    # tests/test_widget_theme_contracts.py's, where slack-bridge is a DASHBOARDS row).
    assert "typeof bridge.onTheme === 'function'" in source
    assert "html,body{margin:0;background:transparent}" in source
    # Refresh never dies silently (no tick limit); the failed read's words are tests/test_slack_bridge_widget_browser.py's.
    assert "max_ticks" not in source
    # No resize protocol of its own: the host measures #root.
    assert "ouro-widget-resize" not in source and "ResizeObserver" not in source


def test_action_kinds_are_named_in_words():
    source = WIDGET.read_text(encoding="utf-8")
    runtime = (SKILL / "lib" / "runtime.py").read_text(encoding="utf-8")
    kinds = set(re.findall(r'operation == "([a-z_]+)"', runtime))
    assert kinds and all(f"{kind}: '" in source for kind in kinds), kinds
    assert "'Slack action'" in source  # an unknown kind gets a generic word, never its raw name


KIT_BYTES = re.compile(rb"/\* transport-kit:begin.*?/\* transport-kit:end \*/", re.S)


def _one_kit(path: Path) -> bytes:
    blocks = KIT_BYTES.findall(path.read_bytes())
    assert len(blocks) == 1, f"{path} must carry exactly one transport kit block, found {len(blocks)}"
    return blocks[0]


def test_shared_kit_is_marked_once():
    _one_kit(WIDGET)


def test_shared_kit_matches_the_telegram_copy_byte_for_byte():
    # The two cards ship in separate PRs and skills install separately: skip only while telegram-bot's card is absent.
    sibling = SKILL.parent / "telegram-bot" / "widget.js"
    if not sibling.exists():
        pytest.skip("telegram-bot's widget.js is not beside this skill")
    assert _one_kit(WIDGET) == _one_kit(sibling)


def test_every_status_field_the_card_reads_exists_on_the_route(tmp_path):
    store = BridgeStore(tmp_path)
    store.set_runtime(
        socket_state="connected", host_adapter_state="active", reaction_context_state="available",
        workspace_name="Northstar Labs", workspace_id="T09NSLABS01", bot_user_id="U09OUROBOT1", bot_id="B09OUROBOT1",
        app_id="A09OUROAPP1", connected_at=1795381200.0, last_event_at=1795384788.0, companion_pid=os.getpid(),
        history_reporting_state="supported", history_reporting_limitation="", reaction_context_limitation="",
        reaction_history_limitation="", presence_identity=1, new_root_channels=["C09OPSROOM1"],
        new_root_channels_error="", last_socket_error="",
        connector_identity={"sends_as": {"id": "U09OUROBOT1", "label": "ouroboros-bot", "kind": "bot"}},
    )
    store.close()
    payload = _read_status_route(_TokensApi(tmp_path))
    source = WIDGET.read_text(encoding="utf-8")
    read = set(re.findall(r"\bdata\.([a-z_]+)", source))
    assert read, "the card reads no status field"
    missing = sorted(field for field in read if field not in payload)
    assert missing == []
    for group, fields in (("link", {"state", "since", "companion_alive"}),
                          ("recent_problems", {"inbound", "outbound_text", "mutations"}),
                          ("outbound_messages", {"waiting", "leased", "held", "delivered", "failed", "uncertain"})):
        assert fields <= set(payload[group])
    for item_kind in ("link", "outcome_unknown", "not_delivered", "not_handled", "held", "reactions", "new_root_channels"):
        assert f"'{item_kind}'" in source or f"item.kind === '{item_kind}'" in source
