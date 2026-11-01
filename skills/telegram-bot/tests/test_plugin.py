import importlib.util
import json
import sys

from telegram_bot.custody import CustodyStore


class FakeApi:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.tasks = []
        self.routes = []
        self.tools = []
        self.tabs = []
        self.settings = []
        self.unload = None

    def get_state_dir(self):
        return str(self.state_dir)

    def get_settings(self, keys):
        assert keys == ["TELEGRAM_PUBLIC_BOT_TOKEN"]
        return {}

    def get_skill_token(self):
        class Token:
            @staticmethod
            def use_in_request():
                return "host-token"

        return Token()

    def register_supervised_task(self, name, handler, **options):
        self.tasks.append((name, handler, options))

    def register_route(self, name, handler, methods):
        self.routes.append((name, handler, methods))

    def register_tool(self, name, handler, **metadata):
        self.tools.append((name, handler, metadata))

    def register_ui_tab(self, tab_id, title, **kwargs):
        self.tabs.append((tab_id, title, kwargs))

    def register_settings_section(self, section_id, **kwargs):
        self.settings.append((section_id, kwargs))

    def on_unload(self, callback):
        self.unload = callback

    def log(self, _level, _message):
        pass


class FakeRequest:
    def __init__(self, payload=None, method="POST"):
        self.payload = payload
        self.method = method

    async def json(self):
        return self.payload


def test_package_style_plugin_registration_and_status_route(tmp_path):
    skill_dir = __import__("pathlib").Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "telegram_bot_extension_test",
        skill_dir / "plugin.py",
        submodule_search_locations=[str(skill_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    api = FakeApi(tmp_path)
    module.register(api)

    assert api.tasks[0][0] == "telegram_presence"
    assert api.tasks[0][2] == {"restart_policy": "on_failure", "max_restarts": 10}
    assert api.routes[0][0] == "status"
    assert api.routes[0][2] == ("GET",)
    assert api.routes[1][0] == "settings/save"
    assert api.routes[1][2] == ("GET", "POST")
    assert api.tabs[0][0] == "transport"
    assert api.tools[0][0] == "telegram_send"
    form = next(c for c in api.tabs[0][2]["render"]["components"] if c["type"] == "form")
    assert form["type"] == "form" and form["route"] == "settings/save"
    assert all(field["name"] != "binding_id" for field in form["fields"])
    assert callable(api.unload)

    # Check the model-visible catalog, not only direct Python handler calls.
    pending = [(name, metadata["schema"]) for name, _, metadata in api.tools]
    while pending:
        path, node = pending.pop()
        if isinstance(node, dict):
            if node.get("type") == "array":
                assert isinstance(node.get("items"), dict) and node["items"], path
            if "enum" in node:
                assert node["enum"] and "" not in node["enum"], path
            pending.extend((f"{path}.{key}", value) for key, value in node.items())
        elif isinstance(node, list):
            pending.extend((f"{path}[{index}]", value) for index, value in enumerate(node))
    operation_schema = next(metadata["schema"] for name, _, metadata in api.tools
                            if name == "telegram_operation")
    assert "parse_mode" not in operation_schema["required"]
    assert operation_schema["properties"]["parse_mode"]["enum"] == ["HTML", "MarkdownV2"]

    response = __import__("asyncio").run(api.routes[0][1](None))
    payload = json.loads(response.body)
    assert payload["runtime_state"] == "not_started"
    assert payload["telegram_offset"] == 0
    assert payload["inbox_failed"] == 0
    assert payload["outbox_failed"] == 0
    metric_paths = {
        component["path"]
        for component in next(
            component
            for component in api.tabs[0][2]["render"]["components"]
            if component.get("title") == "Provider custody"
        )["components"]
    }
    assert {"inbox_failed", "outbox_failed", "outbox_uncertain"} <= metric_paths
    api.unload()


def test_telegram_send_queues_text_and_deduplicates(tmp_path):
    skill_dir = __import__("pathlib").Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "telegram_bot_extension_send_test",
        skill_dir / "plugin.py",
        submodule_search_locations=[str(skill_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    handler = api.tools[0][1]

    first = handler(
        chat_id="-10042",
        text="Hello",
        kind="message",
        topic_id="7",
        request_id="stable-1",
    )
    second = handler(
        chat_id="-10042",
        text="Hello",
        kind="message",
        topic_id="7",
        request_id="stable-1",
    )

    assert first["state"] == "queued"
    assert second["state"] == "already_queued"
    lease = CustodyStore(tmp_path / "custody.sqlite3").claim_outbox()
    assert lease is not None
    assert lease.payload == {
        "kind": "message",
        "chat_id": "-10042",
        "text": "Hello",
        "topic_id": "7",
        "markdown": True,
        "_rendered_chunks": [{"text": "Hello", "parse_mode": "HTML"}],
        "_reporting": {"version": -1, "origin": {"kind": "tool"}},
    }
    api.unload()


def test_telegram_operation_queues_edit_and_reaction_with_new_identity(tmp_path):
    skill_dir = __import__("pathlib").Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "telegram_bot_extension_operation_test", skill_dir / "plugin.py",
        submodule_search_locations=[str(skill_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    operations = {name: handler for name, handler, _metadata in api.tools}
    edit = operations["telegram_operation"](
        method="editMessageText", chat_id="-10042", message_id=9,
        text="corrected", topic_id=7, original_delivery_id="telegram-send:old",
        request_id="op-edit-1",
    )
    reaction = operations["telegram_operation"](
        method="setMessageReaction", chat_id="-10042", message_id=9, topic_id=7,
        reaction=[{"type": "emoji", "emoji": "👍"}], request_id="op-react-1",
    )
    assert edit["state"] == "queued" and reaction["state"] == "queued"
    store = CustodyStore(tmp_path / "custody.sqlite3")
    first = store.claim_outbox()
    assert first is not None and first.payload["kind"] == "operation"
    assert first.payload["method"] == "editMessageText"
    assert first.payload["original_delivery_id"] == "telegram-send:old"
    assert first.payload["_reporting"]["origin"] == {"kind": "tool"}
    receipt_tool = operations["telegram_receipt"]
    assert receipt_tool(operation="operation", request_id="op-edit-1")["state"] == "leased"
    assert receipt_tool(operation="send", request_id="old")["state"] == "not_found"
    store.mark_delivered(first.delivery_id, provider_receipt={"message_id": 9})
    assert receipt_tool(operation="operation", request_id="op-edit-1")["state"] == "delivered"
    second = store.claim_outbox()
    assert second is not None and second.payload["method"] == "setMessageReaction"
    api.unload()


def test_binding_can_be_saved_and_read_through_widget_routes(tmp_path):
    skill_dir = __import__("pathlib").Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "telegram_bot_extension_settings_test",
        skill_dir / "plugin.py",
        submodule_search_locations=[str(skill_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    routes = {name: handler for name, handler, _methods in api.routes}
    binding_id = "a" * 32

    saved = __import__("asyncio").run(
        routes["settings/save"](
            FakeRequest({"binding_id": binding_id, "management_group_id": "-42"})
        )
    )
    status = __import__("asyncio").run(routes["status"](None))

    assert json.loads(saved.body) == {"ok": True, "binding_id": binding_id}
    status_payload = json.loads(status.body)
    assert status_payload["has_presence_binding"] is True
    assert status_payload["binding_state"] == "configured"
    assert status_payload["management_group_id"] == "-42"
    assert (
        json.loads((tmp_path / "settings.json").read_text())["binding_id"] == binding_id
    )
    api.unload()


def test_manifest_widget_declaration_matches_registered_tab(tmp_path):
    """Preflight reads the manifest tab; the live tab is registered by plugin.py."""
    import yaml

    skill_dir = __import__("pathlib").Path(__file__).resolve().parents[1]
    front = yaml.safe_load((skill_dir / "SKILL.md").read_text(encoding="utf-8").split("---")[1])
    spec = importlib.util.spec_from_file_location(
        "telegram_bot_manifest_sync_test", skill_dir / "plugin.py",
        submodule_search_locations=[str(skill_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = FakeApi(tmp_path)
    module.register(api)
    registered = api.tabs[0][2]["render"]
    assert registered == front["ui_tab"]["render"]
    note = registered["components"][0]
    assert note["type"] == "markdown"
    assert "cannot read earlier chat history" in note["text"]
    assert front["contributes"][0]["wakes"] == ["direct_message", "mention", "reply_to_me"]
