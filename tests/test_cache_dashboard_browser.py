"""Run the shipped interval/coverage UI checks; CI requires both real engines."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


def test_cache_dashboard_browser_flows(tmp_path):
    node = os.environ.get("NODE_BIN") or shutil.which("node")
    try:
        import playwright
    except ImportError:
        if os.environ.get("CACHE_DASHBOARD_REQUIRE_RUNTIME") == "1":
            pytest.fail("cache dashboard CI requires Playwright")
        pytest.skip("install Playwright to exercise the real browser flows")
    if not node:
        pytest.fail("cache dashboard browser flows require Node")
    root = Path(__file__).resolve().parents[1]
    module = Path(playwright.__file__).parent / "driver" / "package"
    result = subprocess.run(
        [node, str(root / "skills/cache_efficiency_snapshot/test_widget.cjs"), str(tmp_path)],
        env={**os.environ, "PYTHON": sys.executable, "PLAYWRIGHT_MODULE": str(module)},
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: 8 browser/theme/width flows" in result.stdout
