"""The suite's own guard rails: a clean skip when the core's dependencies are missing, and plugin and
manager checks that fail when the runtime misbehaves."""
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from conftest import CORE_REPO, SKILL, core_available, import_plugin
from phone import manager as mgr


# -- test_controls_pass_the_host_door skips cleanly without starlette --------------------------------------

def test_callproto_host_door_test_skips_without_starlette():
    if not core_available():
        pytest.skip("Ouroboros core checkout not available")
    code = (
        "import importlib.abc, sys, pytest\n"
        "class BlockStarlette(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path, target=None):\n"
        "        if fullname == 'starlette' or fullname.startswith('starlette.'):\n"
        "            raise ModuleNotFoundError(\"No module named 'starlette'\")\n"
        "sys.meta_path.insert(0, BlockStarlette())\n"
        f"rc = pytest.main(['-q', '-p', 'no:cacheprovider', {str(SKILL / 'tests' / 'test_callproto.py')!r}, "
        "'-k', 'test_controls_pass_the_host_door'])\n"
        "sys.exit(rc)\n"
    )
    # The inner run checks a developer's clean skip, so it must not inherit the CI's "a skip is a failure".
    env = {key: value for key, value in os.environ.items() if key != "TELEPHONY_REQUIRE_CORE"}
    env["TELEPHONY_CORE_REPO"] = str(CORE_REPO)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=15)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skipped" in proc.stdout


# -- The tests verify runtime behavior (they fail under runtime mutations) ---------------------------------

def test_plugin_and_manager_checks_fail_under_runtime_mutations(tmp_path, monkeypatch):
    import test_documentation
    import test_manager
    import test_plugin_manifest

    plugin = import_plugin()
    real_register = plugin.register

    # 1. Runtime secret read inside register() must fail test_the_plugin_never_reads_the_sip_password
    def leaky_register(api):
        real_register(api)
        api.get_settings(["TELEPHONY_SIP_PASSWORD"])

    with monkeypatch.context() as mp:
        mp.setattr(plugin, "register", leaky_register)
        with pytest.raises((AssertionError, TypeError)):
            test_plugin_manifest.test_the_plugin_never_reads_the_sip_password(tmp_path / "s1", mp)
            raise RuntimeError("text-only test missed runtime get_settings call")

    # 2. Omitting a runtime tool registration must fail test_hub_place_checks_hold_for_the_manifest
    def incomplete_register(api):
        real_register(api)
        if hasattr(api, "tools") and isinstance(api.tools, dict):
            api.tools.pop("phone_hangup", None)

    with monkeypatch.context() as mp:
        mp.setattr(plugin, "register", incomplete_register)
        with pytest.raises((AssertionError, TypeError)):
            test_plugin_manifest.test_hub_place_checks_hold_for_the_manifest(tmp_path / "s2")
            raise RuntimeError("text-only test missed missing runtime tool registration")

    # 3. Malformed XML in README must fail test_twiml_bins_answer_on_bridge_and_bound_the_ringing
    real_read_text = type(SKILL).read_text

    def broken_readme(self, *args, **kwargs):
        text = real_read_text(self, *args, **kwargs)
        if self.name == "README.md":
            return text.replace("</Sip></Dial>", "</Dial>")  # regex still matched, real XML parser rejects
        return text

    with monkeypatch.context() as mp:
        mp.setattr(type(SKILL), "read_text", broken_readme)
        with pytest.raises(ET.ParseError):
            test_documentation.test_twiml_bins_answer_on_bridge_and_bound_the_ringing()

    # 4. Runtime send_context suggesting end_call in owner_call must fail test_owner_call_rules_never_suggest_end_call
    with monkeypatch.context() as mp:
        mp.setattr(mgr, "OWNER_CALL_RULES", mgr.OWNER_CALL_RULES + " Also call end_call when done.")
        # Restore the constant attribute only if a test reads runtime delivery:
        orig_answered = mgr.CallManager._answered

        async def bad_answered(self, call):
            await orig_answered(self, call)
            if call.peer is not None:
                call.peer.send_context("Please use end_call right now.")

        mp.undo()
        with monkeypatch.context() as mp2:
            mp2.setattr(mgr.CallManager, "_answered", bad_answered)
            with pytest.raises((AssertionError, TypeError)):
                test_manager.test_owner_call_rules_never_suggest_end_call(tmp_path / "s4", mp2)
                raise RuntimeError("constant-only test missed bad runtime send_context")
