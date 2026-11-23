import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("mode,expected", [("baseline", 0), ("regression", 1)])
def test_offline_example_has_deterministic_exit_code(mode, expected):
    root = Path(__file__).resolve().parents[2]
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "server")}
    result = subprocess.run(
        [sys.executable, str(root / "examples/offline-fixture-smoke.py"), mode],
        cwd=root, env=environment, capture_output=True, text=True, timeout=10,
        check=False,
    )
    assert result.returncode == expected, result.stderr
    assert result.stdout == ""


def test_offline_example_rejects_unknown_mode():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "examples/offline-fixture-smoke.py"), "unknown"],
        cwd=root, env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "server")},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 2
