"""Exercise the real host child dispatcher when a core checkout is supplied."""

import os
import json
from pathlib import Path
import subprocess
import sys

import pytest


def test_registered_tools_through_extension_children(tmp_path):
    source = os.environ.get("OUROBOROS_SOURCE_DIR")
    if not source:
        pytest.skip("Set OUROBOROS_SOURCE_DIR to run the host integration test")
    # The suite's safe_test/pytest deadline owns the whole process tree; a
    # subprocess.run timeout would leave extension children orphaned.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("isolated_tool_probe.py")),
         source, str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        env=dict(os.environ, OUROBOROS_DATA_DIR=str(tmp_path / "drive"),
                 OUROBOROS_SETTINGS_PATH=str(tmp_path / "drive" / "settings.json"),
                 OUROBOROS_RUNTIME_MODE="advanced", PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True,
    )
    # Keep successful child output as well as failures: the parent JUnit alone only reports its assertion.
    (tmp_path / 'child-command.json').write_text(json.dumps(result.args))
    (tmp_path / 'child-stdout.txt').write_text(result.stdout)
    (tmp_path / 'child-stderr.txt').write_text(result.stderr)
    (tmp_path / 'child-exit.txt').write_text(str(result.returncode))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verified twelve email tools across the child boundary" in result.stdout


def test_authority_through_real_registry_and_extension_children(tmp_path):
    source = os.environ.get("OUROBOROS_SOURCE_DIR")
    if not source:
        pytest.skip("Set OUROBOROS_SOURCE_DIR to run the Host integration test")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("isolated_authority_probe.py")),
         source, str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        env=dict(os.environ, OUROBOROS_DATA_DIR=str(tmp_path / "drive"),
                 OUROBOROS_SETTINGS_PATH=str(tmp_path / "drive" / "settings.json"),
                 OUROBOROS_RUNTIME_MODE="advanced", PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True, text=True,
    )
    # Keep successful child output as well as failures: the parent JUnit alone only reports its assertion.
    (tmp_path / 'child-command.json').write_text(json.dumps(result.args))
    (tmp_path / 'child-stdout.txt').write_text(result.stdout)
    (tmp_path / 'child-stderr.txt').write_text(result.stderr)
    (tmp_path / 'child-exit.txt').write_text(str(result.returncode))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "zero negative provider writes" in result.stdout
