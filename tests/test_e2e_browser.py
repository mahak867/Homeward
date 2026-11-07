"""Runs the real-browser E2E suite (tests/e2e/browser.e2e.cjs) from pytest.
Skipped automatically when Node.js or Playwright isn't installed."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _playwright_available() -> bool:
    if not shutil.which("node"):
        return False
    r = subprocess.run(["node", "-e", "require('playwright')"], cwd=ROOT, capture_output=True, check=False)
    return r.returncode == 0


@pytest.mark.skipif(not _playwright_available(), reason="Node.js + Playwright not installed")
def test_browser_end_to_end():
    r = subprocess.run(["node", "tests/e2e/browser.e2e.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=600,
                       env={**os.environ, "PYTHON": sys.executable}, check=False)
    print(r.stdout[-4000:])
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
