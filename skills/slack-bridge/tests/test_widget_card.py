"""The Widgets card: one module declaration, a parseable widget.js, bridge-only requests and real status fields."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from lib.store import BridgeStore
from test_status_contract import _read_status_route, _TokensApi
from test_workers_plugin import _Api, _load_plugin

SKILL = Path(__file__).resolve().parents[1]
WIDGET = SKILL / "widget.js"
KIT = re.compile(r"/\* transport-kit:begin.*?/\* transport-kit:end \*/", re.S)
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


NODE = shutil.which("node") or os.environ.get("OUROBOROS_PLAYWRIGHT_NODE_SELECTED") or ""


@pytest.mark.skipif(not NODE or not Path(NODE).exists(), reason="node is not installed")
def test_widget_parses():
    subprocess.run([NODE, "--check", str(WIDGET)], check=True)


def test_widget_follows_the_module_contract():
    source = WIDGET.read_text(encoding="utf-8")
    # Requests go only through the bridge to this skill's own routes.
    urls = set(re.findall(r"['\"](/api/[^'\"]*)['\"]", source))
    assert urls == {"/api/extensions/slack-bridge/status"}
    assert "OuroborosWidget" in source and "bridge.fetch" in source
    assert "XMLHttpRequest" not in source and "WebSocket" not in source
    # Host appearance: theme through the bridge, a light palette, transparent page, disposal.
    assert "typeof bridge.onTheme === 'function'" in source
    assert re.search(r"data-theme\s*=\s*[\"']?light", source)
    assert "html,body{margin:0;background:transparent}" in source
    assert "__ouroWidgetOnDispose" in source
    # Refresh never dies silently: no tick limit, a failed read is said in words with a way to retry.
    assert "max_ticks" not in source and "refresh · showing" in source and "Try again" in source
    # No resize protocol of its own: the host measures #root.
    assert "ouro-widget-resize" not in source and "ResizeObserver" not in source
    # Labels wrap by words; only raw error text may break inside a token.
    assert "word-break:normal" in source and "hyphens:manual" in source
    assert source.count("overflow-wrap:anywhere") == 1 and ".tc-raw{" in source


def test_action_kinds_are_named_in_words():
    source = WIDGET.read_text(encoding="utf-8")
    runtime = (SKILL / "lib" / "runtime.py").read_text(encoding="utf-8")
    kinds = set(re.findall(r'operation == "([a-z_]+)"', runtime))
    assert kinds and all(f"{kind}: '" in source for kind in kinds), kinds
    assert "'Slack action'" in source  # an unknown kind gets a generic word, never its raw name


def test_review_wave_wording_holds():
    source = WIDGET.read_text(encoding="utf-8")
    # F11: token advice lives only in the auth-refusal lines; other connection errors say what happened.
    auth = source[source.index("var AUTH_LINES"):source.index("function itemFor")]
    other = source[source.index("socket_error: ["):source.index("// Only when Slack refused the tokens")]
    assert "SLACK_BOT_TOKEN" in auth and "Accounts and keys" not in other and "token" not in other.lower()
    assert "(item.auth && AUTH_LINES[item.detail])" in source
    # F10: a partly delivered message is never advised to be sent again whole.
    assert "send only the missing parts" in source and "send only what is missing" in source
    # A3/A5: unknown is a dash or "not checked yet".
    assert "'not checked yet'" in source and "!K.known(data.delivery_reports_acked) ? '—'" in source


def test_restart_advice_names_the_place():
    source = WIDGET.read_text(encoding="utf-8")
    assert "turn this skill off and on" not in source
    assert "turn Slack Bridge off and on" in source


def test_shared_kit_is_marked_once():
    assert len(KIT.findall(WIDGET.read_text(encoding="utf-8"))) == 1


def test_shared_kit_matches_the_telegram_copy_when_both_are_present():
    # The two cards ship in separate PRs: compare only once both carry the kit (skills also install separately).
    sibling = SKILL.parent / "telegram-bot" / "widget.js"
    theirs = KIT.search(sibling.read_text(encoding="utf-8")) if sibling.exists() else None
    if theirs is None:
        pytest.skip("telegram-bot's widget.js with the transport kit is not beside this skill")
    assert KIT.search(WIDGET.read_text(encoding="utf-8")).group(0) == theirs.group(0)


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
