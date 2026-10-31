import json

import httpx

from lib.host_adapter import LoopbackPresenceHostAdapter


class OpaqueToken:
    def __init__(self): self.uses = 0
    def __str__(self): raise AssertionError("Do not stringify an opaque skill token")
    def use_in_request(self):
        self.uses += 1
        return "fixture-token"


def test_bind_check_retains_original_target_and_uses_opaque_token_only_for_requests():
    token = OpaqueToken()
    calls = []
    state = ["active"]
    def handler(request):
        calls.append(request)
        assert request.headers["X-Skill-Token"] == "fixture-token"
        if request.url.path == "/identity":
            return httpx.Response(200, json={"places_version": 1, "presence_delivery_version": 1, "presence_place_binding_version": 1})
        params = request.url.params
        if params["operation"] == "bind":
            assert params["require_place"] == "true"
            binding = {"version": 1, "connector": "email-presence", "provider": "email", "account_id": params["account_id"],
                       "conversation_id": params["conversation_id"], "thread_id": params["thread_id"], "place_id": "original-place"}
            return httpx.Response(200, json={"ok": True, "allowed": True, "binding": binding, "state": "active"})
        binding = json.loads(params["binding"])
        assert binding["place_id"] == "original-place"
        assert params["account_id"] == binding["account_id"] == "google:canonical"
        assert params["conversation_id"] == binding["conversation_id"] == "<thread>"
        return httpx.Response(200, json={"ok": True, "allowed": state[0] == "active", "binding": binding,
                                         "state": state[0], "code": "place_disconnected" if state[0] != "active" else ""})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        host = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token=token,
                                           place_http_client=http, sync_only=True)
        binding = host.capture_binding("google:canonical", "<thread>", require_place=True)["binding"]
        assert host.binding_hold(binding, "google:canonical", "<thread>") == ""
        state[0] = "disconnected"
        assert host.binding_hold(binding, "google:canonical", "<thread>") == "Held: place_disconnected"
        assert "original Place binding" in host.binding_hold(None, "google:canonical", "<thread>")
    assert len(calls) == token.uses == 4


def test_legacy_capability_does_not_invent_unbound_snapshot_or_repeat_identity_probe():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == "/identity"
        return httpx.Response(200, json={"presence_delivery_version": 1, "places_version": 1})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        host = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token="fixture",
                                           place_http_client=http, sync_only=True)
        assert host.capture_binding("account", "") == {"supported": False}
        assert host.capture_binding("account", "") == {"supported": False}
    assert len(calls) == 1


def test_legacy_no_place_is_distinct_from_auth_and_outage():
    def handler(request):
        if request.url.path == "/identity":
            return httpx.Response(200, json={"presence_delivery_version": 1, "places_version": 1})
        return httpx.Response(404, json={"code": "place_not_connected", "disposition": "blocked"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        host = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token="fixture",
                                           place_http_client=http, sync_only=True)
        assert "no connected Place" in host.place_hold("account", "<thread>")
