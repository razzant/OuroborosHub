"""Offline subprocess fixture for test_isolated_signing; not a skill entrypoint."""

import json
import os
from pathlib import Path
import shutil
import sys

from workspace_fixtures import nested_document


def main():
    repo, source_skill, temp_root = map(Path, sys.argv[1:])
    os.environ["OUROBOROS_DATA_DIR"] = str(temp_root / "drive")
    os.environ["OUROBOROS_SETTINGS_PATH"] = str(temp_root / "drive" / "settings.json")
    os.environ["OUROBOROS_RUNTIME_MODE"] = "advanced"
    sys.path.insert(0, str(repo))
    import cryptography
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from ouroboros import extension_loader
    from ouroboros.extension_process_runner import dispatch_extension_tool_subprocess, ExtensionProcessError, _RESULT_CAP
    from ouroboros.skill_loader import (
        SkillReviewState, find_skill, save_enabled, save_review_state, save_skill_grants,
    )
    from ouroboros.tools.registry import ToolContext
    from supervisor import queue, state
    from tests._extension_loader_shared import _mark_isolated_deps_installed

    drive = temp_root / "drive"
    drive.mkdir(exist_ok=True)
    state.init(drive)
    queue.init(drive)
    skills = temp_root / "skills"
    skill = skills / "google-workspace"
    skill.mkdir(parents=True)
    for name in ("SKILL.md", "auth.py", "client.py", "results.py", "host_sign_in.py", "user_oauth.py"):
        shutil.copyfile(source_skill / name, skill / name)
    shutil.copyfile(source_skill / "plugin.py", skill / "original_plugin.py")
    shutil.copyfile(source_skill / "tests" / "workspace_fixtures.py", skill / "fixture_data.py")
    # Mock only network I/O; the original plugin, JWT signer, client, manifest,
    # native dependency, registration and process dispatch are all exercised.
    (skill / "plugin.py").write_text('''
import base64
import json
from urllib.parse import parse_qs
import httpx
from . import original_plugin, auth, host_sign_in
from .fixture_data import nested_document

def _old_host(self, method, path, body=None, timeout=40):
    assert (method, path) == ("GET", "/identity")
    return {"ok": True}

host_sign_in.HostSignIn._call = _old_host

_Client = httpx.Client

def _respond(request):
    if str(request.url) == "https://oauth2.googleapis.com/token":
        form = parse_qs(request.content.decode())
        if form.get("grant_type") == ["refresh_token"]:
            assert form["client_id"] == ["fixture-client"]
            assert form["client_secret"] == ["fixture-secret"]
            assert form["refresh_token"] == ["fixture-refresh"]
            return httpx.Response(200, json={"access_token": "user-fixture-token", "expires_in": 3600})
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        assertion = form["assertion"][0]
        head, body, signature = assertion.split(".")
        # Verify the actual signature with the same disposable public key.
        key = serialization.load_pem_private_key(_credential["private_key"].encode(), None)
        key.public_key().verify(base64.urlsafe_b64decode(signature + "=="),
                                (head + "." + body).encode(), padding.PKCS1v15(), hashes.SHA256())
        return httpx.Response(200, json={"access_token": "fixture-token", "expires_in": 3600})
    if request.url.path == "/drive/v3/about":
        email = "user@example.invalid" if request.headers["Authorization"] == "Bearer user-fixture-token" else "fixture@example.invalid"
        return httpx.Response(200, json={"user": {"emailAddress": email, "permissionId": "fixture"}})
    if request.url.path == "/drive/v3/files/fixture_doc":
        return httpx.Response(200, json={"id": "fixture_doc", "name": "Fixture",
                                       "mimeType": "application/vnd.google-apps.document"})
    if request.url.path == "/drive/v3/files/fixture_doc/export":
        return httpx.Response(200, text="Disposable connector test content")
    if request.url.path == "/v1/documents/fixture_nested":
        return httpx.Response(200, json=nested_document())
    if request.url.path == "/v1/documents/fixture_large":
        document = nested_document()
        document["body"]["content"] *= 8
        return httpx.Response(200, json=document)
    raise AssertionError("Unexpected network request: " + str(request.url))

def _client(*args, **kwargs):
    kwargs["transport"] = httpx.MockTransport(_respond)
    return _Client(*args, **kwargs)

def register(api):
    global _credential
    _credential = auth.parse_service_account_info(api.get_settings(["GOOGLE_SERVICE_ACCOUNT_JSON"])["GOOGLE_SERVICE_ACCOUNT_JSON"])
    httpx.Client = _client
    original_plugin.register(api)
    handler = original_plugin._make_docs_read(api)
    def pretty_control(document_id):
        return json.dumps(json.loads(handler(document_id)), indent=2, ensure_ascii=False)
    api.register_tool("fixture_pretty_read", pretty_control,
                      description="Negative control for the child result boundary.",
                      schema={"type": "object", "properties": {"document_id": {"type": "string"}}, "required": ["document_id"]})
''', encoding="utf-8")
    site = skill / ".ouroboros_env" / "python" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True)
    shutil.copytree(Path(cryptography.__file__).parent, site / "cryptography")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    credential = {"type": "service_account", "client_email": "fixture@example.invalid",
                  "token_uri": "https://oauth2.googleapis.com/token",
                  "private_key": key.private_bytes(serialization.Encoding.PEM,
                      serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}
    settings = {"GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(credential),
                "GOOGLE_OAUTH_CLIENT_ID": "fixture-client", "GOOGLE_OAUTH_CLIENT_SECRET": "fixture-secret",
                "GOOGLE_OAUTH_REFRESH_TOKEN": "fixture-refresh"}
    (drive / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    loaded = find_skill(drive, "google-workspace", repo_path=str(skills))
    assert loaded is not None
    save_enabled(drive, loaded.name, True)
    save_review_state(drive, loaded.name, SkillReviewState(status="pass", content_hash=loaded.content_hash))
    permissions = list(loaded.manifest.permissions)
    keys = list(loaded.manifest.env_from_settings)
    save_skill_grants(drive, loaded.name, keys, content_hash=loaded.content_hash,
                      requested_keys=keys, granted_permissions=permissions, requested_permissions=permissions)
    _mark_isolated_deps_installed(drive, loaded)
    loaded = find_skill(drive, loaded.name, repo_path=str(skills))
    error = extension_loader.load_extension(loaded, lambda: settings, drive_root=drive)
    assert error is None, error
    try:
        ctx = ToolContext(repo_dir=repo, drive_root=drive)
        for _ in range(3):
            for name, args in (("workspace_auth_status", {}), ("drive_read_text", {"file_id": "fixture_doc"})):
                tool = extension_loader.get_tool(extension_loader.extension_surface_name(loaded.name, name))
                assert tool["out_of_process"]
                result = json.loads(dispatch_extension_tool_subprocess(tool, ctx, args))
                if name == "workspace_auth_status":
                    assert result["status"] == "ready"
                else:
                    assert result["text"] == "Disposable connector test content"
        print("verified 3 auth and 3 document calls in isolated children")
        for _ in range(3):
            for name, args in (("workspace_auth_status", {}), ("drive_read_text", {"file_id": "fixture_doc"})):
                tool = extension_loader.get_tool(extension_loader.extension_surface_name(loaded.name, name))
                assert tool["out_of_process"]
                result = json.loads(dispatch_extension_tool_subprocess(tool, ctx, dict(args, auth_mode="oauth")))
                if name == "workspace_auth_status":
                    assert result["verified"] and result["refresh_configured"]
                    assert result["actor"]["emailAddress"] == "user@example.invalid"
                else:
                    assert result["text"] == "Disposable connector test content"
        print("verified 3 renewable OAuth auth and 3 document calls in isolated children")
        document = nested_document()
        expected = {"document_id": document["documentId"], "title": document["title"],
                    "revision_id": document["revisionId"], "body": document["body"], "document": document}
        pretty = json.dumps(expected, indent=2, ensure_ascii=False)
        assert len(json.dumps({"ok": True, "result": pretty}, ensure_ascii=False).encode("utf-8")) > _RESULT_CAP
        control = extension_loader.get_tool(extension_loader.extension_surface_name(loaded.name, "fixture_pretty_read"))
        try:
            dispatch_extension_tool_subprocess(control, ctx, {"document_id": "fixture_nested"})
        except ExtensionProcessError as exc:
            assert "exceeded safety cap" in str(exc), str(exc)
        else:
            raise AssertionError("Pretty result must exceed the real child boundary")
        tool = extension_loader.get_tool(extension_loader.extension_surface_name(loaded.name, "docs_read"))
        assert tool["out_of_process"]
        result = dispatch_extension_tool_subprocess(tool, ctx, {"document_id": "fixture_nested"})
        assert json.loads(result) == expected
        assert len(json.dumps({"ok": True, "result": result}, ensure_ascii=False).encode("utf-8")) < _RESULT_CAP
        print("verified complete compact Docs result across the child boundary; pretty control rejected")
        # Unlike the compact regression above, even compact JSON is oversized.
        # The real isolated child must retain full bytes before its IPC cap.
        from ouroboros.artifacts import read_actor_source_bytes
        from ouroboros.tools.core_file_tools import _read_file
        ctx.task_id = "large-provider-result"
        large = json.loads(dispatch_extension_tool_subprocess(tool, ctx, {"document_id": "fixture_large"}))
        assert large["stored"] and large["actor_readable"] and large["result_complete"]
        raw = read_actor_source_bytes(drive, ctx.task_id, large["source_ref"])
        complete = json.loads(raw)
        assert len(complete["document"]["body"]["content"]) == 2800
        assert len(raw) > _RESULT_CAP
        assert "Полный текст строки" in _read_file(ctx, **large["read"]["arguments"])
        print("verified oversized full JSON retained and actor-readable across the real child boundary")
    finally:
        extension_loader.unload_extension(loaded.name)


if __name__ == "__main__":
    main()
