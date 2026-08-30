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

    def on_unload(self, callback):
        self.unload = callback

    def log(self, _level, _message):
        pass


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

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

    assert api.tasks[0][0] == "telegram_presence_transport"
    assert api.tasks[0][2] == {"restart_policy": "on_failure", "max_restarts": 10}
    assert api.routes[0][0] == "status"
    assert api.routes[0][2] == ("GET",)
    assert api.routes[1][0] == "settings/save"
    assert api.routes[1][2] == ("POST",)
    assert api.tabs[0][0] == "transport"
    assert api.tools[0][0] == "telegram_send"
    form = api.tabs[0][2]["render"]["components"][0]
    assert form["type"] == "form" and form["route"] == "settings/save"
    assert "conversation_id=*" in form["fields"][0]["help"]
    assert callable(api.unload)

    response = __import__("asyncio").run(api.routes[0][1](None))
    payload = json.loads(response.body)
    assert payload["runtime_state"] == "not_started"
    assert payload["telegram_offset"] == 0
    assert payload["inbox_failed"] == 0
    assert payload["outbox_failed"] == 0
    metric_paths = {
        component["path"]
        for component in api.tabs[0][2]["render"]["components"][1]["components"][1][
            "components"
        ]
    }
    assert {"inbox_failed", "outbox_failed"} <= metric_paths
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
    }
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
        routes["settings/save"](FakeRequest({"binding_id": binding_id}))
    )
    status = __import__("asyncio").run(routes["status"](None))

    assert json.loads(saved.body) == {"ok": True, "binding_id": binding_id}
    status_payload = json.loads(status.body)
    assert status_payload["has_presence_binding"] is True
    assert status_payload["binding_state"] == "configured"
    assert (
        json.loads((tmp_path / "settings.json").read_text())["binding_id"] == binding_id
    )
    api.unload()
